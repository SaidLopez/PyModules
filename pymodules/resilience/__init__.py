"""
Resilience middleware for PyModules.

The ``pymodules.resilience`` package collects the cross-cutting resilience
concerns expressed as middleware: rate limit, circuit breaker, retry, DLQ,
and fallback. Each concern lives in its own module so a regression in one
area lands in a single file.

The factory ``default_middleware(...)`` (and its env-reading sibling) is
the single ergonomic surface for assembling the standard chain;
``ModuleHostConfig`` itself holds only a plain ``list[Middleware]``.
"""

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerMiddleware,
    CircuitBreakerOpen,
    CircuitState,
)
from .dlq import DeadLetterEntry, DeadLetterQueue, DLQMiddleware
from .factory import default_middleware, default_middleware_from_env
from .fallback import Fallback, FallbackMiddleware
from .idempotency import IdempotencyMiddleware, IdempotencyStore, InMemoryIdempotencyStore
from .rate_limit import RateLimitExceeded, RateLimitMiddleware
from .retry import RetryMiddleware, RetryPolicy

__all__ = [
    # Rate limiting
    "RateLimitExceeded",
    "RateLimitMiddleware",
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerMiddleware",
    "CircuitBreakerOpen",
    "CircuitState",
    # Retry
    "RetryMiddleware",
    "RetryPolicy",
    # Idempotency
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    # Dead Letter Queue
    "DLQMiddleware",
    "DeadLetterEntry",
    "DeadLetterQueue",
    # Fallback
    "Fallback",
    "FallbackMiddleware",
    # Factory
    "default_middleware",
    "default_middleware_from_env",
]
