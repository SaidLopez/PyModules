# PyModules

A small in-process **command-dispatch** core for Python with an in-process **event bus** for fan-out, plus optional contrib packages for transport, persistence, discovery, and observability. Inspired by NetModules, but explicit about being command-shaped rather than event-shaped at the dispatch layer.

## Project identity

**Core** is the dispatch primitive plus the `Middleware` contract and the small set of in-process middlewares that ship by default. Concretely: registration, type-routed dispatch with a single winning handler per call, the `(command, next) -> response` chain, and the resilience/observability middlewares that depend only on the standard library (rate limit, circuit breaker, retry, in-memory DLQ, fallback, metrics, tracing, lifecycle).

**Contrib** is anything that crosses a process boundary or pulls an external dependency: REST router (FastAPI), DB repository (SQLAlchemy), message broker (Redis/Kafka), service discovery (Consul/DNS), persistent DLQ, OTel exporter, JWT auth. Each is optional, swappable, and gated behind an install extra.

A user who wants only the dispatch core must not be forced to reason about Redis, Consul, or SQLAlchemy — but they get the in-process middleware set without an extra, because the `Middleware` contract is what the core *is*, and shipping zero default middleware would force every user to reinvent retry/metrics/correlation-IDs.

## Language

**Command**:
A named request with a typed request payload and a typed response. Exactly one **Module** claims it via the type-routed registry; the **Module** returns the response. Carries a typed `context: CommandContext` (see entry) that middleware reads and writes for observability. Optionally carries a caller-supplied `command_id` for idempotency: a second dispatch with the same id (while the entry remains in the idempotency store) returns the cached response without re-invoking the handler.
_Avoid_: Event, message, request (when ambiguous).

**CommandRequest**:
The typed input payload carried into the handler.
_Avoid_: EventInput, args, params.

**CommandResponse**:
The typed value returned by the handler. Handlers **return** their response; they do not mutate the **Command**.
_Avoid_: EventOutput, result, return value (when ambiguous).

**CommandContext**:
The typed cross-cutting context dataclass carried on every **Command** and **Event**: `trace_id`, `correlation_id`, `parent_span_id` (each `str | None`), plus an `extra: dict[str, Any]` escape hatch for genuinely ad-hoc keys. Replaces the previous untyped `meta: dict[str, Any]` god-bag — observability middleware reads and writes typed fields, not string keys.
_Avoid_: meta (now removed), metadata (overloaded with `ModuleMetadata`), envelope.

**Module**:
A handler component whose methods are individually decorated with `@handles(CommandClass)` to claim **Commands**. Each decorated method takes the typed **CommandRequest** and returns the typed **CommandResponse**. Registered with a **ModuleHost**.
_Avoid_: Plugin, handler-class, service (overloaded).

**ModuleHost**:
The dispatcher that owns the **Module** registry and runs each **Command** through a configurable middleware chain. Holds no broker or persistence concerns.
_Avoid_: Bus, broker, registry (these are distinct concepts elsewhere in the codebase).

**Middleware**:
A composable unit `(command, next) -> response` that wraps dispatch. Resilience (rate limit, circuit breaker, retry, DLQ), observability (metrics, tracing), and authorization are all expressed as middleware. The terminal middleware looks up the **Module** in the registry and calls it.
_Avoid_: Interceptor, hook, decorator (when ambiguous).

**Dispatch**:
The act of running a **Command** through the host's middleware chain to the claiming **Module** and returning its response. Synchronous (`dispatch`) or asynchronous (`dispatch_async`); sync dispatch raises if the claiming **Module** is async — there is no implicit loop bridging.
_Avoid_: Handle (overloaded with the module method), send, emit.

**Event** (distinct from Command):
A fire-and-forget broadcast notification. Has no return payload and no winning handler — N subscribers may receive it. Carries the same `context: CommandContext` shape as a **Command** so a publisher can propagate the active trace into the events it emits. Two transports: the in-process **EventBus** (core) for same-process listeners, and an external broker (contrib `messaging`) when listeners live in another process. The same `Event` subclass is used in both cases; the transport is the orthogonal axis.
_Avoid_: Command, message (when ambiguous).

**EventBus**:
The in-process pub/sub registry owned by **ModuleHost**. Maps `type(event) → list[subscriber]` for O(1) routing on `publish`. Errors raised by one subscriber are logged and isolated — other subscribers still receive the event. Has no middleware chain (fire-and-forget; see ADR-0007).
_Avoid_: Broker (overloaded with the cross-process case), dispatcher (overloaded with ModuleHost).

**Subscribe**:
The act of registering a callable to receive instances of a given **Event** subclass. Expressed either via `@subscribes(EventClass)` on a Module method (auto-wired at `host.register(module)`) or by calling `host.event_bus.subscribe(EventClass, handler)` directly. Routing is exact-type: a subscriber to a base class does not receive a derived event.
_Avoid_: Listen-to, observe (when ambiguous).

**Publish**:
The act of sending an **Event** to subscribers. In-process: `host.publish(event)` (sync) or `host.publish_async(event)` (async) routes through the **EventBus**. Cross-process: a Module holds its own broker reference and calls the broker directly. Distinct from **dispatch**.

## Relationships

- A **ModuleHost** owns zero or more **Modules**, an ordered list of **Middleware**, and one in-process **EventBus**.
- A **Module** declares the **Commands** it handles by decorating methods with `@handles(CommandClass)` (one method per **Command**); the host scans the class at registration and builds an O(1) `{CommandClass → bound method}` dispatch table.
- A **Module** declares the **Events** it subscribes to by decorating methods with `@subscribes(EventClass)`; the host wires those methods onto its **EventBus** at registration. A Module may have any mix of `@handles` and `@subscribes` methods.
- At most one **Module** may claim any given **Command** class. Registering a second module that claims the same class raises unless `override=True` is passed. **No such restriction applies to Events**: multiple Modules may subscribe to the same `EventClass`, which is the whole point of pub/sub fan-out.
- Routing is type-only — for both **Commands** and **Events**. There is no `can_handle` / `can_subscribe` predicate, and Event routing does not walk the inheritance chain.
- A **Command** is dispatched to exactly one **Module** and returns a response; an **Event** is published to zero or more subscribers and returns nothing.
- In-process delivery: **dispatch** runs the **Command** through the middleware chain; **publish** runs the **Event** through the **EventBus** (no middleware chain, errors isolated per subscriber). Cross-process publish remains broker-owned and Module-owned — `ModuleHost` itself never crosses a process boundary.
- A **Module** holds no back-reference to its **ModuleHost**. Inside a handler, a Module does not re-enter dispatch. Cross-Module fan-out is either (a) caller-orchestrated — the caller dispatches command 1, inspects the response, dispatches command 2 — or (b) broadcast via **Event** publish (in-process via the **EventBus**, or out-of-process via a broker held by the Module). Re-entering the middleware chain from within a handler would double-charge rate-limit tokens, re-arm retries, and hide the call graph from the configured chain; it is intentionally not supported. Publishing an **Event** is permitted because it does not re-enter the middleware chain.

## Layout

- `pymodules.{host,module,interfaces,middleware,eventbus,config,exceptions,logging}` — **dispatch core + in-process EventBus**, ~1,000 LOC, standard library only.
- `pymodules.{resilience,tracing}` — **default in-process middlewares**, ~1,200 LOC, standard library only. Re-exported from the top-level `pymodules` namespace for ergonomics.
- `pymodules.contrib.{api,db,messaging,discovery,health,auth,tracing}` — **contrib**, each gated behind an install extra; pulls third-party deps (FastAPI, SQLAlchemy, Redis, Consul, OpenTelemetry, …). The `messaging` package is the **cross-process** Event transport; the in-process EventBus in core does not depend on it.

## Example dialogue

> **Dev:** "If I want `CreateUser` to also notify the audit log, do I register two modules with `can_handle(CreateUser)`?"
> **Maintainer:** "No — `CreateUser` is a **Command**; only one Module wins. Have the user-creation Module **publish** a `UserCreated` **Event** after it succeeds — `host.publish(UserCreated(...))`. The audit log subscribes to that Event with `@subscribes(UserCreated)`. Everything stays in-process via the **EventBus**; if you later need an audit listener in another process, the same `UserCreated` dataclass is published over the contrib broker."

## Flagged ambiguities

- "Event" historically referred to the in-process command. Resolved (ADR-0001): the in-process *dispatch* primitive is a **Command**. Subsequently (ADR-0007) "Event" was promoted to a first-class core primitive *for pub/sub*, distinct from Command — with an in-process **EventBus** transport in core and an optional broker transport in contrib. "Event" therefore now names a broadcast notification of either transport; the *dispatch* primitive remains **Command**, exclusively.
- "handle" is overloaded between `Module.handle(command)` and the host's old `host.handle(event)` method. Resolved: host method renamed to `dispatch`; `Module.handle` keeps its name as the per-module callback.
- "output" was a mutable field on the in-process command. Resolved: handlers **return** their response; **Command** carries only the request payload and metadata.

## Non-goals

- **Auto-generated REST routing from class names.** Class names are an internal concern; URLs are an external contract. REST endpoints are declared explicitly via `@api_endpoint(method=..., path=...)` on each **Command**. The contrib API layer does no convention magic.
- **Implicit sync↔async bridging.** Sync `dispatch()` will not silently run an async handler under `asyncio.run()`; it raises.
- **Broker-aware host.** A **ModuleHost** never publishes to an external **broker**. Modules that need cross-process publish hold their own broker reference. The host does own an in-process **EventBus** (no transport, no serialisation, no persistence) — that is a fan-out registry, not a broker.
- **In-handler dispatch back to the host.** A **Module** has no `host` back-pointer. Handlers do not call `self.host.dispatch(OtherCommand)` — re-entering the middleware chain from within a handler would double-charge rate-limit tokens, re-arm retries, and hide the call graph. Fan-out is caller-orchestrated or broker-mediated.
