# Caller-supplied `command_id` for idempotency

Production callers that dispatch over an unreliable boundary — HTTP retries, queue redelivery, SDK auto-retry — need at-most-once handler execution per logical request. The middleware chain already has `RetryMiddleware`, but retry without de-duplication is unsafe for non-idempotent handlers: a successful side effect followed by a connection drop will re-execute on retry.

We added an optional `command_id: str | None` field on `Command` and a new `IdempotencyMiddleware` that caches successful responses keyed by `command_id`. A second dispatch with the same id (within the store's retention window) returns the cached response without invoking the handler; concurrent dispatches with the same id are serialised behind a per-key `asyncio.Lock` so the handler runs exactly once. The store is a `Protocol` so contrib packages can supply Redis/SQL backends; the bundled `InMemoryIdempotencyStore` is TTL-bounded.

The id is **caller-supplied**, not framework-generated. Auto-generating from a request hash was rejected because two semantically different dispatches with identical request payloads (e.g., "transfer £10 from A to B" issued twice deliberately) must be allowed to execute twice. Idempotency is a property of the caller's intent, not of the payload, and the caller is the only party that knows whether the second dispatch is a retry or a fresh request.

The id lives as a first-class field on `Command`, not in `Command.meta`. The meta dict is already an untyped god-bag; adding load-bearing semantics to a meta key would have made the contract invisible to mypy and impossible to grep. `command_id` is a typed, optional, documented attribute.

`IdempotencyMiddleware` sits at the outermost layer of `default_middleware`'s standard chain: a cached hit returns before rate-limit tokens are consumed, breaker state is touched, or retry runs. Duplicate work is not "work" — counting it against rate limits would penalise the caller for the framework's deduplication.

## Consequences

- Only **successful** responses are cached. Exceptions propagate uncached so a transient failure does not become a permanent error for the entire TTL window — a subsequent dispatch with the same id re-runs the handler. Handlers that need "negative result caching" must encode the failure as a successful response shape (e.g., `OrderResponse(status="rejected")`), not as a raised exception.
- The bundled store is in-memory and lazy-evicted on access; no background reaper runs. Single-process deployments are correct; multi-process / multi-pod deployments must supply a shared store (Redis-backed contrib) or accept that each process maintains its own cache. The factory's `idempotency_ttl=` kwarg only configures the in-memory store — callers wanting a custom store build the middleware list by hand.
- The middleware maintains per-key locks in a `WeakValueDictionary` so locks are garbage-collected once no caller is holding them. Memory footprint is bounded by *concurrent* (not historical) ids. The store itself is bounded by TTL.
- `command_id=None` is a pass-through. Existing user code that constructs `Command(name=..., request=...)` without an id continues to work; idempotency is fully opt-in per-dispatch.
- A future enhancement might cache a *request fingerprint* alongside the response and refuse to replay if a later dispatch presents the same `command_id` with a different payload — protecting against caller bugs where the id is reused for a different request. Not in v1; the trade-off is added store size vs caught bugs, and we have no production data to justify it yet.
