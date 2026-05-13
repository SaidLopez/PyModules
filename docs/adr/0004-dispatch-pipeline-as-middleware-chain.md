# Dispatch pipeline is a middleware chain

`ModuleHost.handle()` had grown into a 70-line method that interleaved rate-limit checks, circuit-breaker checks, retry-with-backoff, DLQ writes, metrics counters, correlation-ID injection, lifecycle callbacks, in-progress tracking, and the actual module call — with the async variant being a near-duplicate. Adding any new cross-cutting concern (auth, idempotency, audit, caching) required editing both methods or threading state through `on_event_start`/`on_event_end`/`on_error` callbacks. The host had become a junk drawer of orthogonal concerns.

We restructured dispatch as a middleware chain: each cross-cutting concern is a `(command, next) -> response` callable. `ModuleHost` composes the chain once at construction; `dispatch()` and `dispatch_async()` each become ~5 lines that invoke the chain. The chain is async-first; the sync entry point is a thin wrapper that raises if the resolved module is async (see ADR-0005-equivalent in CONTEXT.md: no implicit loop bridging).

Default middleware (rate limit, circuit breaker, retry, metrics, tracing, terminal dispatch) ship in `pymodules.resilience` / `pymodules.tracing` and are wired by `ModuleHostConfig` for convenience. Users can replace the chain wholesale or insert custom middleware between defaults.

## Consequences

- `ModuleHostConfig` shifts from a list of pre-baked feature flags (`rate_limiter=…`, `circuit_breaker=…`, `retry_policy=…`) to a `middleware: list[Middleware]` field. The old fields are removed in the same commit — no transitional sugar period. A `default_middleware(rate_limit=…, circuit_breaker_threshold=…, …)` factory is the single sugar surface; building two parallel kwargs paths on `ModuleHostConfig` and on the factory would drift apart and produce ambiguity when both are passed.
- Per-concern state asymmetry: middleware that owns purely operational state folds the state inline (`RateLimitMiddleware` owns its token bucket directly — the standalone `RateLimiter` class is removed). Middleware whose state is externally observable wraps a state-holding object so user code can hold a reference (`CircuitBreakerMiddleware(breaker: CircuitBreaker)`, `DLQMiddleware(queue: DeadLetterQueue)`). Stateless concerns receive a config dataclass (`RetryMiddleware(policy: RetryPolicy)`, `FallbackMiddleware(...)`).
- New contrib packages express their value as middleware (`pymodules.contrib.auth.AuthMiddleware`, `pymodules.contrib.api.TracingMiddleware`, etc.). The dispatch loop in core never needs to learn about them.
- Lifecycle callbacks (`on_event_start`, `on_event_end`, `on_error`) become a single `LifecycleMiddleware` rather than three host-level fields.
- The implementation cost is real — middleware composition is more machinery than the current straight-line code — but the current code already had these abstractions as hidden state, expressed badly. Making them explicit is the point.
