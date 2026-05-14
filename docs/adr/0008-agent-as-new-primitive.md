# `Agent` as a new primitive, sibling of `Module`

`Module` is a passive claimant (ADR-0003): each registered class waits for a `Command` to land on its type-routed dispatch slot and returns a response. The framework provides no primitive for an **active producer** — a long-running, stateful actor that *initiates* work by dispatching `Commands` and publishing `Events` on its own schedule. Such actors had nowhere to live in the existing model: representing them as `Module`s would require re-entering dispatch from a handler (explicitly forbidden by the non-goal "in-handler dispatch back to the host"), and representing them as external scripts placed them outside the framework entirely — losing the lego-block ergonomics that motivate the whole project.

The driving use case is the "super agents" idea: register one class, spawn many AI-shaped runs that hold goals, decide for themselves, dispatch on their own, and publish progress events as they go. None of that is expressible as a `Module`.

We add `Agent` as a sibling of `Module`. The two primitives share registration ergonomics (a class registered with the `ModuleHost`) but differ in role: `Module` *claims*, `Agent` *initiates*.

## Decision

**Template / instance split.** The `Agent` class itself is the *template* — registered once, declares constraints, capabilities, and lifecycle hooks. At runtime the host holds zero-to-many running `AgentRun` *instances* of the same template. Each `AgentRun` owns its own state; concurrent runs of the same template do not share state. The final names — `Agent` (template) and `AgentRun` (instance) — are chosen for consistency with the codebase's existing `Module` / `Command` naming (no `Template` suffix anywhere) and with industry usage in pipelines/workflows ("workflow run", "pipeline run", "job run").

**Three trigger modes, declared on the template:**

- **Explicit spawn.** A caller (a Command handler, an external API, an admin tool) requests an `AgentRun` of a given template, optionally with constructor arguments. The mechanical surface is `host.spawn(AgentTemplate, **kwargs) -> AgentRun`.
- **Event subscription.** The template declares `@subscribes(EventClass)` methods (same decorator `Module` already uses); each matching event published to the host's `EventBus` triggers a step in an `AgentRun`.
- **Schedule.** The template declares an interval or cron; the host's scheduler executes a step per tick.

Accepting all three is a deliberate choice over a tighter single-trigger primitive. The cost — a primitive without a one-sentence definition, of the kind ADRs 0001/0003/0006 worked to remove from the codebase — is acknowledged. If the shape proves unwieldy in production, a future ADR splits it into narrower primitives.

**Runtime shape: optional `run()` task + callback methods, sharing state.** If the `Agent` class defines a `run()` method, the host launches it as a task when the `AgentRun` starts. `@subscribes(EventClass)` and `@scheduled(...)` methods on the same class fire independently and share the instance's state. Pure-callback Agents (no `run()`) are alive between triggers; pure-`run()` Agents (no callbacks) work as autonomous loops. The hybrid shape expresses all three trigger modes without forcing ceremony on any of them — and lets a single Agent both react to events *and* run a background loop (e.g., periodic progress checks). Pure-callback-only and pure-`run()`-only alternatives were rejected: the former cannot express the "AI agent decides for itself" case; the latter nests every event handler in awaits.

**Default lifetime: alive until `self.stop()` or host shutdown.** AgentRuns are persistent by default — one AgentRun handles many events / ticks over its life, not one per trigger. Users wanting one-AgentRun-per-Event opt in by calling `self.stop()` at the end of their handler. This matches the framework's existing "explicit > automatic" stance.

**State model: pluggable `AgentStateStore` Protocol, in-memory default.** Mirrors the precedent set by `IdempotencyStore` in ADR-0005: the framework defines a `Protocol` for state I/O; v1 ships a bundled in-memory implementation; persistent backends (Redis, SQL) live in contrib. Agents opt into a persistent store per-template. Per-instance state only in v1: no template-shared state, no cross-AgentRun state, no global state. Users wanting cross-instance state route it through a Module or an external store, avoiding race conditions on shared template attributes. Mandatory-persistent state (every `set_attr` checkpoints) was rejected as too heavy a tax on the common case.

**Concurrency: unlimited by default, optional per-template `max_concurrent`, raise on overflow.** No global cap. A template may declare `max_concurrent: int | None`. When the cap is hit, `host.spawn(...)` raises `AgentSpawnRejected` at the call site. Queueing on overflow was rejected: it bakes a job-queue's worth of edge cases (priority, timeout, dead-letter on queue overflow) into a primitive most users won't reach, and a separate queueing primitive can be added later if pressure justifies it.

**Failure policy: log, terminate, publish `AgentFailed`, opt-in restart.** An unhandled exception in `run()` or in any callback logs the error, terminates the `AgentRun`, and publishes an `AgentFailed` Event to the host's `EventBus` so other Agents/Modules can react (alert, retry orchestration, escalation). Auto-restart is opt-in via a `restart_policy: RetryPolicy` attribute on the template, reusing the existing `RetryPolicy` shape from `RetryMiddleware`. Propagating the exception to the caller of `spawn()` was rejected — by the time an AgentRun fails, the spawning call has long since returned, so re-raising on its frame is impossible without storing futures the user must explicitly await.

**Spawn API: three callers, three surfaces.**

- External code (the user's main script, an API endpoint handler) holds the `ModuleHost` directly and calls `host.spawn(AgentTemplate, **kwargs) -> AgentRun`.
- An `Agent` holds a host back-reference by design (this ADR's deliberate exception to ADR-0003), and can call `self.host.spawn(...)`.
- A `Module` does **not** get a host back-reference (preserving ADR-0003). Modules that need to spawn inject a narrow `AgentSpawner` Protocol at construction time, the same way they inject `EventBus` for publish today. The `AgentSpawner` exposes only `spawn(template, **kwargs) -> AgentRun` — no dispatch, no other host surface.

**Host chain only; no per-Agent middleware chain.** An `AgentRun` that dispatches a `Command` flows through the host's existing middleware chain exactly once. No additional outer chain is composed per template. The credible alternative — each Agent template owning its own chain so "super-agent constraints" are framework-enforced — is rejected for v1 in favour of simplicity. Capability whitelists, token budgets, and max-iteration caps are enforced inside the Agent's own class body; the framework provides no isolation primitive for them. A follow-up ADR may add per-Agent chains if production demand justifies the second composition site.

**Relationship to ADR-0003.** ADR-0003 explicitly forbids a `Module` from holding a back-pointer to its `ModuleHost` and from re-entering dispatch from within a handler. That prohibition is intact for `Module` — handlers do not call `self.host.dispatch(...)`, because re-entering the middleware chain *from inside a chain frame* would double-charge rate limits and re-arm retries. An `AgentRun` is **not inside a chain frame**: it runs on its own task / thread / event-loop step, entering the host's chain as a fresh top-level call. The accounting concerns ADR-0003 raised do not apply, and an Agent therefore *does* hold a host reference. This is the deliberate exception, recorded here rather than weakening ADR-0003's text.

## Consequences

- `Agent` joins the public `pymodules` surface alongside `Module`, `Command`, `Event`. `CONTEXT.md` grows two glossary terms (`Agent`, `AgentRun`).
- `ModuleHost.register(...)` accepts both `Module` and `Agent` instances. An internal scheduler is added (lazily — only constructed if any registered Agent declares a schedule).
- A new `host.spawn(AgentTemplate, **kwargs) -> AgentRun` API materialises the explicit-spawn trigger. The existing dispatch path is unchanged.
- Cancellation semantics (graceful vs. hard cancel on `self.stop()` / host shutdown) and lifecycle hooks (`on_start`, `on_stop`, `on_error` — whether named methods on the template or framework-emitted Events) remain to settle in a small follow-up. v1 default: `self.stop()` sets a flag the `run()` task is expected to honour at its next checkpoint; host shutdown cancels all in-flight AgentRuns with a configurable grace period.
- "Constraints of the module" — the phrasing that motivated the per-Agent-chain alternative — does not become a framework feature in this ADR. Constraints are class-internal. Users who need hard framework-enforced isolation must wait for the follow-up ADR or build their own wrapping middleware on the host chain (keying on the dispatching actor's identity).
- `AgentFailed` is a new framework-emitted `Event` that any Module or Agent can subscribe to. It carries the failed template, the AgentRun id, and the exception. It is published synchronously into the in-process EventBus so subscriber failures don't propagate (per ADR-0007).
- `AgentSpawner` becomes a new public Protocol alongside `EventBus`, exported from the `pymodules` namespace. The bundled implementation is host-internal; users pass `host.agent_spawner` to Modules that need to spawn.
- Existing tests, examples, and docs that frame `Module` as the only first-class building block require updates to mention `Agent` as the active-producer sibling.
