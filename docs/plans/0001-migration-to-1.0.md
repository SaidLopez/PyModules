# Migration plan: PyModules 1.0

Implements ADR-0001 through ADR-0004 plus the smaller policy decisions captured in `CONTEXT.md` (no `host.publish`, explicit-only REST routing, no sync↔async bridging, duplicate-claim guard).

This is a 1.0 break. No compatibility shims — the renames are the point. Each commit below leaves the test suite green; ordering is chosen so that bisect lands on a single concept per failure.

## Test policy

Tests are rewritten in place commit-by-commit, not maintained as a parallel suite. Each commit's diff includes the test edits needed to keep the suite green. Test file reorganization (mirroring the resilience subpackage split) happens in commit 6 alongside the source restructure, not as a separate commit.

## Ordering principle

Smallest-blast-radius changes first, then vocabulary, then handler contract, then the largest internal refactor, then deletions. Specifically:

1. Drop host-side broker awareness before moving messaging out — keeps the contrib move purely mechanical.
2. Do the contrib namespace move while every symbol still has its old name — one kind of churn per diff.
3. Rename `Event → Command` before changing the handler contract — pure rename is auditable; semantic change isn't.
4. Split the registry change from the return-response change — two distinct host-side mechanisms, two distinct bisect targets, even though user code touches both at once.
5. Land the middleware chain after the handler contract is final — composing middleware over a mutate-in-place protocol would be wasted work.
6. Deletions last (conventions, `pymodules.fastapi` shim) — once nothing depends on them, removal is a one-liner.

## The commits

### 1. Remove `host.publish()` and broker/registry fields from core

**Scope.**
- Delete `ModuleHost.publish()` and any internal broker reference held by the host.
- Delete `ModuleHostConfig.message_broker` and `ModuleHostConfig.service_registry`.
- Delete the `PYMODULES_BROKER_TYPE`, `PYMODULES_REDIS_URL`, and `PYMODULES_DISCOVERY_TYPE` reading from `ModuleHostConfig.from_env()` (currently `config.py:163-194`). Core no longer knows about Redis or Consul. Each contrib's existing `*Config.from_env()` (`RedisBrokerConfig.from_env()`, `ConsulRegistryConfig.from_env()`, `DNSRegistryConfig.from_env()`) remains the only env entry point for those concerns.
- Delete `Metrics.events_published` (its only increment site is inside `ModuleHost.publish()` and dies with it).
- Update tests and examples that called `host.publish(...)` to construct a broker directly and call `broker.publish(...)`.
- The `pymodules.messaging` package itself stays where it is for now.

**Why first.** Decouples the host from messaging before messaging moves namespace. After this commit, `pymodules.host` has no transitive dependency on the messaging package.

**Verify.**
- `grep -rn "host\.publish\|message_broker\|service_registry" pymodules/ tests/` returns nothing.
- `python -c "from pymodules.host import ModuleHost"` does not import `pymodules.messaging`.
- Full test suite green.

---

### 2. Move integrations to `pymodules.contrib.*`

**Scope.**
- `pymodules/api/` → `pymodules/contrib/api/`
- `pymodules/db/` → `pymodules/contrib/db/`
- `pymodules/discovery/` → `pymodules/contrib/discovery/`
- `pymodules/messaging/` → `pymodules/contrib/messaging/`
- `pymodules/health.py` → `pymodules/contrib/health.py`
- Add `pymodules/contrib/__init__.py` (empty; namespace marker, not a re-export hub).
- Update `pyproject.toml` extras to point at the new paths; extras names unchanged (`pymodules[api]`, etc.).
- Update all internal imports, tests, examples, and docs.
- `pymodules/fastapi/` stays in place as a deprecation shim, but its imports now point at `pymodules.contrib.api`. The shim's `__init__.py` emits `DeprecationWarning` on import, naming `pymodules.contrib.api` as the replacement. The warning lives through commits 2–7 and the shim is deleted in commit 8.
- `pymodules/__init__.py` does **not** re-export anything from `contrib.*`.

**Why second.** Zero behavior change — pure layout shift. Decoupled from every semantic change so the diff is reviewable as a rename-only operation. Done after commit 1 so the move doesn't drag broker-coupling into contrib.

**Verify.**
- `from pymodules import ModuleHost` does not trigger any optional dependency import (test by running in a venv with no extras installed).
- Each contrib subpackage is import-isolated: `import pymodules.contrib.api` fails cleanly with the gated extra message if FastAPI isn't installed, rather than at `import pymodules`.
- Full test suite green.

---

### 3. Rename `Event → Command` (vocabulary only)

**Scope.**
- `Event` → `Command`; `EventInput` → `CommandRequest`; `EventOutput` → `CommandResponse`.
- `ModuleHost.handle()` → `ModuleHost.dispatch()`; async variant → `dispatch_async()`.
- Host-state renames: `_events_in_progress` dict → `_commands_in_progress`; public property `events_in_progress` → `commands_in_progress`.
- `on_event_start`/`on_event_end`/`on_error` keep their names for now (they go away in commit 6).
- **Out of scope for this commit:** `Metrics` field names. The whole `Metrics` class is observability, not core vocab — it gets absorbed into `MetricsMiddleware` in commit 6 and deleted from core. Renaming its `events_*` fields here would be renaming-to-delete; skip it.
- Update all tests, examples, and docs.

**Why third, and why on its own.** This is the most user-visible diff in the migration. Keeping it free of any semantic change means a user upgrading can do a mechanical find-and-replace on their codebase, run their tests, and have one clean checkpoint before they start rewriting handlers.

**Verify.**
- `grep -rni "event" pymodules/ | grep -v contrib/messaging | grep -v "events_"` (the `events_*` carve-out covers the `Metrics` fields still on the host) returns only references to the cross-process `Event` concept (or none).
- `Module.handle` is the only surviving `handle` symbol on the framework side.
- Full test suite green.

---

### 4. Type-routed registry with duplicate-claim guard

**Scope.**
- Introduce `@handles(CommandClass)` method decorator. One decorated method per **Command** class; the decorator writes a `__pymodules_handles__` marker onto the method.
- `ModuleHost.register(module)` scans the instance's class for `@handles`-marked methods and builds a `{CommandClass → bound method}` dispatch table.
- Registering a second module whose decorated methods claim an already-claimed command class raises `DuplicateCommandError` (or similar). `register(module, override=True)` permits deliberate replacement.
- Dispatch resolves the bound handler method in O(1) by `type(command)`. No predicate post-filter.
- Drop the iterate-every-module dispatch loop. Drop the single-`handle(self, cmd)` method on the `Module` base class — the base class survives only as a typed `self.host` accessor.
- Drop `Module.can_handle` entirely (currently `@abstractmethod` at `module.py:92`). Update every example and test to remove its `can_handle` override; the isinstance bodies become dead surface.

**Why fourth.** Independent of how the handler returns its response — this commit changes only how the host finds the handler. Splitting from commit 5 means a registration bug and a return-value bug land in different commits.

**Verify.**
- New test: registering two modules that both claim `FooCommand` raises.
- New test: `register(SecondModule(), override=True)` succeeds and the second module wins.
- New test: dispatch latency is O(1) in number of registered modules (assert via call counter on `can_handle`, not wall time).
- Full test suite green.

---

### 5. Handlers return their response

**Scope.**
- Change `Module.handle` signature from `(self, command) -> None` to `(self, command: Cmd) -> Response` — but with Option C from §2, the single `handle` method is already gone; each `@handles(C)`-decorated method has its own typed signature.
- Drop `Command.output` and `Command.handled`. `Command` carries `request`, `name`, `meta` only.
- Type the dispatch surface strictly. `Command` stays `Generic[Req, Resp]` (rename of today's `Event[I, O]`). `dispatch(cmd: Command[Req, Resp]) -> Resp` and `dispatch_async(cmd: Command[Req, Resp]) -> Awaitable[Resp]` propagate the response type. The `@handles(C)` decorator carries `TypeVar`s so mypy verifies the decorated method's signature matches `C`'s parameters.
- Host uses the handler's return value as the dispatch result; no mutation of `command`.
- Remove the early-break-on-`handled` logic from the dispatch loop (the loop is already gone after commit 4, but stragglers may remain).
- Update every internal handler in tests and examples.

**Why fifth, and why split from commit 4.** Different code path, different failure modes. A user upgrading will do commits 4 and 5 as one rewrite in their codebase, but framework-side they're independently bisectable.

**Verify.**
- `Command` has no `output` or `handled` attribute (assert via `dir()` or a dedicated test).
- Handler return value round-trips through `dispatch()` unchanged.
- Full test suite green.

---

### 6. Dispatch pipeline as middleware chain

**Scope.**
- Define `Middleware = Callable[[Command, Callable[[Command], Awaitable[Response]]], Awaitable[Response]]`.
- `ModuleHost` composes the chain once at construction; `dispatch_async()` invokes the chain; `dispatch()` is a thin sync wrapper.
- Sync `dispatch()` raises `SyncDispatchOnAsyncHandlerError` (or similar) if the resolved module's `handle` is a coroutine function. No `asyncio.run()` fallback.
- Split `pymodules/resilience.py` into a subpackage `pymodules/resilience/` with per-concern files: `rate_limit.py`, `circuit_breaker.py`, `retry.py`, `dlq.py`, `fallback.py`, `factory.py`. The `__init__.py` re-exports the public surface so `from pymodules.resilience import RateLimitMiddleware` keeps working unchanged. The split lands in the same commit as the middleware conversion (one coherent diff per concern; the same files would be touched twice if deferred).
- Mirror in tests: `tests/test_resilience.py` → `tests/resilience/test_rate_limit.py` etc.
- Move `OpenTelemetryExporter` (currently at `pymodules/tracing.py:321`) to `pymodules/contrib/tracing/opentelemetry.py`. Core `pymodules.tracing` keeps `Tracer`, `TraceContext`, `Span`, `generate_id`, `inject_trace_context`, `extract_trace_context`, and the module-level accessors — nothing that imports the `opentelemetry` package.
- Convert resilience features to middleware: `RateLimitMiddleware`, `CircuitBreakerMiddleware`, `RetryMiddleware`, `DLQMiddleware`, `FallbackMiddleware` in `pymodules.resilience`. Per-concern state asymmetry:
  - `RateLimitMiddleware(rate=100, burst=10)` — owns token-bucket state directly. **Delete the standalone `RateLimiter` class** — it has zero callers outside the host today.
  - `CircuitBreakerMiddleware(breaker: CircuitBreaker)` — wraps a state-holding `CircuitBreaker`. Class survives because its state machine is externally observable.
  - `RetryMiddleware(policy: RetryPolicy)` — middleware holds the stateless config dataclass.
  - `DLQMiddleware(queue: DeadLetterQueue)` — wraps a state-holding queue. Class survives because users drain/inspect/replay it.
  - `FallbackMiddleware(...)` — stateless; fold inline.
- Convert tracing/metrics to middleware: `TracingMiddleware`, `MetricsMiddleware` in `pymodules.tracing`. **Delete the `Metrics` dataclass and the `ModuleHost.metrics` property from core** — counters become `MetricsMiddleware` internal state, with each resilience middleware owning its own counters (e.g. `RateLimitMiddleware.rejected_count`). Drop the host's `_commands_in_progress` dict and the public `commands_in_progress` property at the same time (debug surface, not a metric — if needed, reintroduce as a dedicated `InProgressTrackingMiddleware`).
- Lifecycle callbacks (`on_event_start`/`on_event_end`/`on_error`) → single `LifecycleMiddleware`. Remove the three fields from `ModuleHostConfig`.
- Terminal middleware looks up the module in the type-routed registry and calls it.
- `ModuleHostConfig` gains `middleware: list[Middleware]` and a separate `default_middleware(rate_limit=…, circuit_breaker_threshold=…, retry_max=…, dlq_size=…, …)` factory in `pymodules.resilience`. **Old convenience flags (`rate_limiter=`, `circuit_breaker=`, `retry_policy=`, `dead_letter_queue=`, `tracer=`, `enable_metrics`, `enable_tracing`) are deleted in this commit** — no transitional sugar. The factory is the single sugar surface; the config dataclass holds only `middleware` plus the non-resilience fields (`max_workers`, `propagate_exceptions`, `log_level`).
- Add env-reading siblings: `pymodules.resilience.default_middleware_from_env()` reads `PYMODULES_RATE_LIMIT*`, `PYMODULES_CIRCUIT_BREAKER_*`, `PYMODULES_RETRY_*`, `PYMODULES_DLQ_*`; `pymodules.tracing.middleware_from_env()` reads tracing env vars. These move out of `ModuleHostConfig.from_env()`, which keeps only `PYMODULES_MAX_WORKERS`, `PYMODULES_PROPAGATE_EXCEPTIONS`, `PYMODULES_LOG_LEVEL`.

**Why sixth.** Biggest internal change in the migration. Lands after the handler contract is final so middleware composes over `(command) -> response`, not over mutate-in-place. Independent commit because the diff is large and the risk surface is the dispatch hot path.

**Verify.**
- New test: middleware ordering is observable and configurable.
- New test: custom middleware inserted between defaults runs in the declared position.
- New test: sync `dispatch()` on an async handler raises, does not silently bridge.
- Existing resilience tests pass through the middleware indirection.
- Full test suite green.

---

### 7. Drop auto REST routing conventions

**Scope.**
- Delete `pymodules/contrib/api/conventions.py` (426 LOC) entirely. Pure delete — nothing in the file survives the CONTEXT.md non-goal "no convention magic." If a function inside it turns out to be genuinely independent of class-name-to-URL routing, rescue it into `pymodules/contrib/api/errors.py` or a new helper module, but the default presumption is delete.
- Trim references to it from `pymodules/contrib/api/router.py`. The `@api_endpoint(method=..., path=...)` decorator (`pymodules/contrib/api/decorators.py`), HTTP error mapping (`errors.py`), and the router itself all survive.
- REST routing is exclusively via `@api_endpoint(method=..., path=...)` on each Command class.
- Update examples that relied on implicit class-name-to-URL derivation.
- Document the change in `pymodules/contrib/api/__init__.py` and `README.md`.

**Why seventh.** Decoupled from the host changes; lands after contrib has settled. Deleting before commit 6 would have meant churning two API surfaces simultaneously.

**Verify.**
- `grep -rn "conventions" pymodules/contrib/api/` returns nothing.
- Every example endpoint has an explicit `@api_endpoint` decorator.
- Full test suite green.

---

### 8. Delete `pymodules.fastapi` shim

**Scope.**
- Remove `pymodules/fastapi/` entirely.
- Remove the `fastapi` entry from `pyproject.toml` extras (if it existed as a distinct extra).
- Confirm no internal references remain.

**Why last.** Frees the namespace; trivial diff once everything else has migrated. Doing this earlier risks blocking a user mid-migration who hasn't yet switched their imports.

**Verify.**
- `find pymodules -name fastapi` returns nothing.
- `python -c "import pymodules.fastapi"` raises `ModuleNotFoundError`.
- Full test suite green.

## What's deliberately not in this plan

- **Splitting into multiple PyPI distributions** (`pymodules-redis`, `pymodules-sqlalchemy`, etc.). ADR-0002 leaves this open as a future mechanical step; the contrib layout makes it possible but it's not required for 1.0.
- **A NetModules migration guide.** ADR-0001 notes newcomers from NetModules need orientation; that's a docs task, not a code commit, and belongs after 1.0 ships.
- **Per-middleware configuration ergonomics** (e.g., a fluent builder for the default chain). The plain `list[Middleware]` is the authoritative model; sugar can come later without another breaking change.

## Release mechanics

- Each commit above is a single PR. Reviewers see one concept per diff.
- Tag `1.0.0` after commit 8.
- The `0.x` line gets no further releases; the README links to this plan as the upgrade path.
