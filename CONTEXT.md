# PyModules

A small in-process **command-dispatch** core for Python, with optional contrib packages for transport, persistence, discovery, and observability. Inspired by NetModules, but explicit about being command-shaped rather than event-shaped.

## Project identity

**Core** is the dispatch primitive plus the `Middleware` contract and the small set of in-process middlewares that ship by default. Concretely: registration, type-routed dispatch with a single winning handler per call, the `(command, next) -> response` chain, and the resilience/observability middlewares that depend only on the standard library (rate limit, circuit breaker, retry, in-memory DLQ, fallback, metrics, tracing, lifecycle).

**Contrib** is anything that crosses a process boundary or pulls an external dependency: REST router (FastAPI), DB repository (SQLAlchemy), message broker (Redis/Kafka), service discovery (Consul/DNS), persistent DLQ, OTel exporter, JWT auth. Each is optional, swappable, and gated behind an install extra.

A user who wants only the dispatch core must not be forced to reason about Redis, Consul, or SQLAlchemy — but they get the in-process middleware set without an extra, because the `Middleware` contract is what the core *is*, and shipping zero default middleware would force every user to reinvent retry/metrics/correlation-IDs.

## Language

**Command**:
A named request with a typed request payload and a typed response. Exactly one **Module** claims it via the type-routed registry; the **Module** returns the response.
_Avoid_: Event, message, request (when ambiguous).

**CommandRequest**:
The typed input payload carried into the handler.
_Avoid_: EventInput, args, params.

**CommandResponse**:
The typed value returned by the handler. Handlers **return** their response; they do not mutate the **Command**.
_Avoid_: EventOutput, result, return value (when ambiguous).

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
A fire-and-forget broadcast notification published to an external broker. Has no return payload and no winning handler — N subscribers may receive it.
_Avoid_: Command, message (when ambiguous).

**Publish**:
The act of sending an Event to the external message broker. Distinct from dispatch.

## Relationships

- A **ModuleHost** owns zero or more **Modules** and an ordered list of **Middleware**.
- A **Module** declares the **Commands** it handles by decorating methods with `@handles(CommandClass)` (one method per **Command**); the host scans the class at registration and builds an O(1) `{CommandClass → bound method}` dispatch table.
- At most one **Module** may claim any given **Command** class. Registering a second module that claims the same class raises unless `override=True` is passed.
- Routing is type-only. There is no `can_handle` predicate. Conditional handling is expressed by splitting the **Command** into narrower classes or branching inside the handler body.
- A **Command** is dispatched to exactly one **Module**; an **Event** is published to zero or more subscribers.
- Dispatch is in-process; Publish crosses the process boundary via a broker. The **ModuleHost** does not publish — `Module` code calls a broker directly.
- A **Module** holds no back-reference to its **ModuleHost**. Inside a handler, a Module does not re-enter dispatch. Cross-Module fan-out is either (a) caller-orchestrated — the caller dispatches command 1, inspects the response, dispatches command 2 — or (b) broadcast via **Event** publish to a broker. Re-entering the middleware chain from within a handler would double-charge rate-limit tokens, re-arm retries, and hide the call graph from the configured chain; it is intentionally not supported.

## Layout

- `pymodules.{host,module,interfaces,middleware,config,exceptions,logging}` — **dispatch core**, ~900 LOC, standard library only.
- `pymodules.{resilience,tracing}` — **default in-process middlewares**, ~1,200 LOC, standard library only. Re-exported from the top-level `pymodules` namespace for ergonomics.
- `pymodules.contrib.{api,db,messaging,discovery,health,auth,tracing}` — **contrib**, each gated behind an install extra; pulls third-party deps (FastAPI, SQLAlchemy, Redis, Consul, OpenTelemetry, …).

## Example dialogue

> **Dev:** "If I want `CreateUser` to also notify the audit log, do I register two modules with `can_handle(CreateUser)`?"
> **Maintainer:** "No — `CreateUser` is a **Command**; only one Module wins. Have the user-creation Module **publish** a `UserCreated` **Event** after it succeeds. The audit log subscribes to that Event."

## Flagged ambiguities

- "Event" historically referred to the in-process command. Resolved: the in-process primitive is a **Command**; "Event" is reserved for cross-process broadcast notifications.
- "handle" is overloaded between `Module.handle(command)` and the host's old `host.handle(event)` method. Resolved: host method renamed to `dispatch`; `Module.handle` keeps its name as the per-module callback.
- "output" was a mutable field on the in-process command. Resolved: handlers **return** their response; **Command** carries only the request payload and metadata.

## Non-goals

- **Auto-generated REST routing from class names.** Class names are an internal concern; URLs are an external contract. REST endpoints are declared explicitly via `@api_endpoint(method=..., path=...)` on each **Command**. The contrib API layer does no convention magic.
- **Implicit sync↔async bridging.** Sync `dispatch()` will not silently run an async handler under `asyncio.run()`; it raises.
- **Broker-aware host.** A **ModuleHost** never publishes to an external broker. Modules that need to publish hold their own broker reference.
- **In-handler dispatch back to the host.** A **Module** has no `host` back-pointer. Handlers do not call `self.host.dispatch(OtherCommand)` — re-entering the middleware chain from within a handler would double-charge rate-limit tokens, re-arm retries, and hide the call graph. Fan-out is caller-orchestrated or broker-mediated.
