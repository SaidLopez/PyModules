"""
Factory helpers for building the default middleware chain.

``default_middleware(...)`` is the single sugar surface — the config
dataclass holds only ``middleware: list[Middleware]`` plus non-resilience
fields. Convenience flags on ``ModuleHostConfig`` (``rate_limiter=``,
``circuit_breaker=``, etc.) were removed in the 1.0 migration.
"""

import os

from ..middleware import Middleware
from .circuit_breaker import CircuitBreaker, CircuitBreakerMiddleware
from .dlq import DeadLetterQueue, DLQMiddleware
from .idempotency import IdempotencyMiddleware, InMemoryIdempotencyStore
from .rate_limit import RateLimitMiddleware
from .retry import RetryMiddleware, RetryPolicy


def default_middleware(
    *,
    idempotency_ttl: float | None = None,
    rate_limit: float | None = None,
    rate_limit_burst: int = 10,
    rate_limit_block: bool = False,
    circuit_breaker_threshold: int | None = None,
    circuit_breaker_timeout: float = 30.0,
    retry_max: int | None = None,
    retry_base_delay: float = 1.0,
    dlq_size: int | None = None,
    enable_tracing: bool = False,
    enable_metrics: bool = False,
) -> list[Middleware]:
    """
    Build a list of middleware in the standard order:

        idempotency → rate_limit → circuit_breaker → retry → dlq → tracing → metrics

    Each concern is opt-in via its kwarg; passing ``None`` (or 0 for
    integer thresholds) omits that middleware. The terminal middleware is
    added by the host itself and is not part of this list.

    Idempotency sits at the outermost layer so a cached hit returns
    before rate-limit tokens are consumed, breaker state is touched, or
    retry runs. For non-default stores (Redis, SQL), build the chain by
    hand and prepend ``IdempotencyMiddleware(your_store)`` yourself —
    ``idempotency_ttl`` only configures the bundled in-memory store.
    """
    chain: list[Middleware] = []

    if idempotency_ttl is not None and idempotency_ttl > 0:
        chain.append(IdempotencyMiddleware(InMemoryIdempotencyStore(ttl_seconds=idempotency_ttl)))

    if rate_limit is not None and rate_limit > 0:
        chain.append(
            RateLimitMiddleware(
                rate=rate_limit,
                burst=rate_limit_burst,
                block=rate_limit_block,
            )
        )

    if circuit_breaker_threshold is not None and circuit_breaker_threshold > 0:
        breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            recovery_timeout=circuit_breaker_timeout,
        )
        chain.append(CircuitBreakerMiddleware(breaker))

    if retry_max is not None and retry_max > 0:
        chain.append(
            RetryMiddleware(RetryPolicy(max_retries=retry_max, base_delay=retry_base_delay))
        )

    if dlq_size is not None and dlq_size > 0:
        chain.append(DLQMiddleware(DeadLetterQueue(max_size=dlq_size)))

    # Tracing/metrics live in pymodules.tracing; import lazily so the core
    # resilience surface does not pull tracing in unconditionally.
    if enable_tracing:
        from ..tracing import TracingMiddleware

        chain.append(TracingMiddleware())

    if enable_metrics:
        from ..tracing import MetricsMiddleware

        chain.append(MetricsMiddleware())

    return chain


def default_middleware_from_env() -> list[Middleware]:
    """
    Same as ``default_middleware`` but parameterised via environment vars:

      - ``PYMODULES_IDEMPOTENCY_TTL``
      - ``PYMODULES_RATE_LIMIT``, ``PYMODULES_RATE_LIMIT_BURST``
      - ``PYMODULES_CIRCUIT_BREAKER_THRESHOLD``, ``PYMODULES_CIRCUIT_BREAKER_TIMEOUT``
      - ``PYMODULES_RETRY_MAX``, ``PYMODULES_RETRY_BASE_DELAY``
      - ``PYMODULES_DLQ_SIZE``
      - ``PYMODULES_ENABLE_TRACING``, ``PYMODULES_ENABLE_METRICS``
    """
    idempotency_ttl = float(os.getenv("PYMODULES_IDEMPOTENCY_TTL", "0"))
    rate_limit = float(os.getenv("PYMODULES_RATE_LIMIT", "0"))
    rate_limit_burst = int(os.getenv("PYMODULES_RATE_LIMIT_BURST", "10"))
    cb_threshold = int(os.getenv("PYMODULES_CIRCUIT_BREAKER_THRESHOLD", "0"))
    cb_timeout = float(os.getenv("PYMODULES_CIRCUIT_BREAKER_TIMEOUT", "30"))
    retry_max = int(os.getenv("PYMODULES_RETRY_MAX", "0"))
    retry_base_delay = float(os.getenv("PYMODULES_RETRY_BASE_DELAY", "1.0"))
    dlq_size = int(os.getenv("PYMODULES_DLQ_SIZE", "0"))
    enable_tracing = os.getenv("PYMODULES_ENABLE_TRACING", "false").lower() == "true"
    enable_metrics = os.getenv("PYMODULES_ENABLE_METRICS", "false").lower() == "true"

    return default_middleware(
        idempotency_ttl=idempotency_ttl if idempotency_ttl > 0 else None,
        rate_limit=rate_limit if rate_limit > 0 else None,
        rate_limit_burst=rate_limit_burst,
        circuit_breaker_threshold=cb_threshold if cb_threshold > 0 else None,
        circuit_breaker_timeout=cb_timeout,
        retry_max=retry_max if retry_max > 0 else None,
        retry_base_delay=retry_base_delay,
        dlq_size=dlq_size if dlq_size > 0 else None,
        enable_tracing=enable_tracing,
        enable_metrics=enable_metrics,
    )


__all__ = ["default_middleware", "default_middleware_from_env"]
