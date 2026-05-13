"""
Circuit breaker pattern.

``CircuitBreaker`` holds the externally observable state machine.
``CircuitBreakerMiddleware`` wraps it for dispatch.

The class survives the middleware refactor because its state machine is
externally observable: user code drains, inspects, and resets it.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any

from ..exceptions import PyModulesSignal
from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

resilience_logger = get_logger("resilience")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreakerOpen(PyModulesSignal):
    """
    Raised when the circuit breaker is open.

    Subclass of ``PyModulesSignal`` so the host re-raises it as-is regardless
    of ``propagate_exceptions`` — breaker rejection is a framework decision,
    not a handler error.
    """

    pass


@dataclass
class CircuitBreaker:
    """
    Circuit breaker state machine.

    Prevents cascading failures by stopping requests to failing services.
    Externally observable: user code may inspect ``state``, read counters,
    and reset.

    Attributes:
        failure_threshold: Failures in CLOSED before transitioning to OPEN.
        recovery_timeout: Seconds in OPEN before testing HALF_OPEN.
        success_threshold: Successes in HALF_OPEN needed to close.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    success_threshold: int = 2

    def __post_init__(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state (lazily transitions on read)."""
        with self._lock:
            self._check_state_transition()
            return self._state

    def _check_state_transition(self) -> None:
        if self._state == CircuitState.OPEN and self._last_failure_time:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                resilience_logger.info("Circuit breaker transitioning to HALF_OPEN")

    def record_success(self) -> None:
        """Record a successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    resilience_logger.info("Circuit breaker CLOSED after recovery")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                resilience_logger.warning("Circuit breaker OPEN after half-open failure")
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    resilience_logger.warning(
                        "Circuit breaker OPEN after %d failures", self._failure_count
                    )

    def allow_request(self) -> bool:
        """Check whether a request should be allowed through."""
        with self._lock:
            self._check_state_transition()
            # CLOSED and HALF_OPEN both pass; HALF_OPEN lets the test
            # request through and uses its outcome to decide the next state.
            return self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def __call__(self, func: Callable) -> Callable:
        """Decorator form: wrap an arbitrary callable with the breaker."""

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self.allow_request():
                raise CircuitBreakerOpen(
                    f"Circuit breaker is OPEN. Retry after {self.recovery_timeout}s"
                )
            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception:
                self.record_failure()
                raise

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self.allow_request():
                raise CircuitBreakerOpen(
                    f"Circuit breaker is OPEN. Retry after {self.recovery_timeout}s"
                )
            try:
                result = await func(*args, **kwargs)
                self.record_success()
                return result
            except Exception:
                self.record_failure()
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper

    def reset(self) -> None:
        """Reset to CLOSED with cleared counters."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None


class CircuitBreakerMiddleware:
    """
    Middleware adapter around a ``CircuitBreaker``.

    Holds the breaker by reference so user code can inspect its state.

    Attributes:
        breaker: The ``CircuitBreaker`` whose state machine is consulted.
        rejected_count: Number of commands rejected with the circuit open.
    """

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker
        self.rejected_count = 0

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        if not self.breaker.allow_request():
            self.rejected_count += 1
            resilience_logger.warning("Circuit breaker open for command %s", command.name)
            raise CircuitBreakerOpen("Circuit breaker is open")
        try:
            result = await next_call(command)
        except Exception:
            self.breaker.record_failure()
            raise
        else:
            self.breaker.record_success()
            return result


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerMiddleware",
    "CircuitBreakerOpen",
    "CircuitState",
]
