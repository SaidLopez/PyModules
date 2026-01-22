"""
Resilience patterns for PyModules framework.

Provides rate limiting, circuit breaker, retry logic, and dead letter queue.
"""

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .interfaces import Event

from .logging import get_logger

resilience_logger = get_logger("resilience")


# =============================================================================
# Rate Limiting
# =============================================================================


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter.

    Attributes:
        rate: Maximum events per second.
        burst: Maximum burst size (bucket capacity).
        block: If True, block until tokens available. If False, raise RateLimitExceeded.

    Example:
        limiter = RateLimiter(rate=100, burst=10)
        if limiter.acquire():
            process_event()
    """

    rate: float = 100.0  # events per second
    burst: int = 10  # max burst size
    block: bool = False  # block or raise

    def __post_init__(self) -> None:
        self._tokens: float = float(self.burst)
        self._last_update: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_update = now

    def acquire(self, tokens: int = 1) -> bool:
        """
        Acquire tokens from the bucket.

        Args:
            tokens: Number of tokens to acquire.

        Returns:
            True if tokens were acquired.

        Raises:
            RateLimitExceeded: If block=False and no tokens available.
        """
        with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True

            if self.block:
                # Calculate wait time
                wait_time = (tokens - self._tokens) / self.rate
                time.sleep(wait_time)
                self._refill()
                self._tokens -= tokens
                return True

            # Calculate retry_after
            retry_after = (tokens - self._tokens) / self.rate
            raise RateLimitExceeded(
                f"Rate limit exceeded. Retry after {retry_after:.2f}s",
                retry_after=retry_after,
            )

    async def acquire_async(self, tokens: int = 1) -> bool:
        """Async version of acquire."""
        with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True

            if self.block:
                wait_time = (tokens - self._tokens) / self.rate
                await asyncio.sleep(wait_time)
                self._refill()
                self._tokens -= tokens
                return True

            retry_after = (tokens - self._tokens) / self.rate
            raise RateLimitExceeded(
                f"Rate limit exceeded. Retry after {retry_after:.2f}s",
                retry_after=retry_after,
            )

    def reset(self) -> None:
        """Reset the rate limiter to full capacity."""
        with self._lock:
            self._tokens = float(self.burst)
            self._last_update = time.monotonic()


# =============================================================================
# Circuit Breaker
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open."""

    pass


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern implementation.

    Prevents cascading failures by stopping requests to failing services.

    Attributes:
        failure_threshold: Number of failures before opening circuit.
        recovery_timeout: Seconds to wait before testing recovery.
        success_threshold: Successes needed in half-open to close circuit.

    Example:
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30)

        @breaker
        def call_external_service():
            ...
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
        """Current circuit state."""
        with self._lock:
            self._check_state_transition()
            return self._state

    def _check_state_transition(self) -> None:
        """Check if state should transition."""
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
                # Reset failure count on success
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open reopens the circuit
                self._state = CircuitState.OPEN
                resilience_logger.warning("Circuit breaker OPEN after half-open failure")
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    resilience_logger.warning(
                        "Circuit breaker OPEN after %d failures", self._failure_count
                    )

    def allow_request(self) -> bool:
        """Check if a request should be allowed."""
        with self._lock:
            self._check_state_transition()

            if self._state == CircuitState.CLOSED:
                return True
            elif self._state == CircuitState.HALF_OPEN:
                return True  # Allow test request
            else:
                return False

    def __call__(self, func: Callable) -> Callable:
        """Decorator to wrap a function with circuit breaker."""

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
        """Reset circuit breaker to closed state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None


# =============================================================================
# Retry Logic
# =============================================================================


@dataclass
class RetryPolicy:
    """
    Retry policy with exponential backoff.

    Attributes:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        exponential_base: Base for exponential backoff.
        retryable_exceptions: Tuple of exception types to retry.

    Example:
        policy = RetryPolicy(max_retries=3, base_delay=1.0)

        @policy
        def flaky_operation():
            ...
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number."""
        delay = self.base_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)

    def should_retry(self, exception: Exception, attempt: int) -> bool:
        """Check if should retry given exception and attempt number."""
        if attempt >= self.max_retries:
            return False
        return isinstance(exception, self.retryable_exceptions)

    def __call__(self, func: Callable) -> Callable:
        """Decorator to wrap a function with retry logic."""

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(self.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if not self.should_retry(e, attempt):
                        raise

                    delay = self.calculate_delay(attempt)
                    resilience_logger.warning(
                        "Retry attempt %d/%d after %.2fs: %s",
                        attempt + 1,
                        self.max_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)

            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry state")

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(self.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if not self.should_retry(e, attempt):
                        raise

                    delay = self.calculate_delay(attempt)
                    resilience_logger.warning(
                        "Retry attempt %d/%d after %.2fs: %s",
                        attempt + 1,
                        self.max_retries,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)

            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry state")

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper


# =============================================================================
# Dead Letter Queue
# =============================================================================


@dataclass
class DeadLetterEntry:
    """An entry in the dead letter queue."""

    event: "Event"
    error: Exception
    timestamp: float = field(default_factory=time.time)
    attempts: int = 1
    module_name: str = ""


class DeadLetterQueue:
    """
    Dead letter queue for failed events.

    Stores events that failed processing for later inspection or reprocessing.

    Example:
        dlq = DeadLetterQueue(max_size=1000)

        # Events are added automatically by ModuleHost when configured
        # Later, inspect or reprocess:
        for entry in dlq.entries:
            print(f"Failed: {entry.event.name} - {entry.error}")

        # Reprocess all
        dlq.reprocess(host)
    """

    def __init__(
        self,
        max_size: int = 1000,
        on_add: Callable[[DeadLetterEntry], None] | None = None,
    ):
        """
        Initialize dead letter queue.

        Args:
            max_size: Maximum number of entries to keep.
            on_add: Optional callback when entry is added.
        """
        self._entries: deque[DeadLetterEntry] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._on_add = on_add
        self.max_size = max_size

    def add(
        self,
        event: "Event",
        error: Exception,
        module_name: str = "",
        attempts: int = 1,
    ) -> DeadLetterEntry:
        """Add a failed event to the queue."""
        entry = DeadLetterEntry(
            event=event,
            error=error,
            module_name=module_name,
            attempts=attempts,
        )

        with self._lock:
            self._entries.append(entry)

        resilience_logger.warning(
            "Event %s added to DLQ after %d attempts: %s",
            event.name,
            attempts,
            error,
        )

        if self._on_add:
            try:
                self._on_add(entry)
            except Exception as e:
                resilience_logger.error("DLQ on_add callback failed: %s", e)

        return entry

    @property
    def entries(self) -> list[DeadLetterEntry]:
        """Get all entries in the queue."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        """Get number of entries in the queue."""
        with self._lock:
            return len(self._entries)

    def clear(self) -> int:
        """Clear all entries. Returns number of entries cleared."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def pop(self) -> DeadLetterEntry | None:
        """Remove and return the oldest entry."""
        with self._lock:
            if self._entries:
                return self._entries.popleft()
            return None

    def reprocess(
        self,
        handler: Callable[["Event"], "Event"],
        max_entries: int | None = None,
    ) -> tuple[int, int]:
        """
        Reprocess entries from the queue.

        Args:
            handler: Function to handle events (e.g., host.handle).
            max_entries: Maximum entries to process (None = all).

        Returns:
            Tuple of (successful, failed) counts.
        """
        successful = 0
        failed = 0
        processed = 0

        while True:
            if max_entries and processed >= max_entries:
                break

            entry = self.pop()
            if entry is None:
                break

            processed += 1
            try:
                # Reset event state
                entry.event.handled = False
                entry.event.output = None

                handler(entry.event)

                if entry.event.handled:
                    successful += 1
                    resilience_logger.info(
                        "Successfully reprocessed event %s from DLQ",
                        entry.event.name,
                    )
                else:
                    # No handler found, re-add to DLQ
                    self.add(
                        entry.event,
                        RuntimeError("No handler found on reprocess"),
                        attempts=entry.attempts + 1,
                    )
                    failed += 1
            except Exception as e:
                # Failed again, re-add with incremented attempts
                self.add(
                    entry.event,
                    e,
                    module_name=entry.module_name,
                    attempts=entry.attempts + 1,
                )
                failed += 1

        return successful, failed


# =============================================================================
# Fallback
# =============================================================================


@dataclass
class Fallback:
    """
    Fallback handler for graceful degradation.

    Provides a fallback value or function when the primary operation fails.

    Example:
        fallback = Fallback(default_value={"status": "unavailable"})

        @fallback
        def get_user_data():
            return external_api.get_user()

        # Or with a function:
        fallback = Fallback(fallback_func=lambda: cached_data)
    """

    default_value: Any = None
    fallback_func: Callable[[], Any] | None = None
    exceptions: tuple[type[Exception], ...] = field(default_factory=lambda: (Exception,))
    log_errors: bool = True

    def get_fallback(self) -> Any:
        """Get the fallback value."""
        if self.fallback_func:
            return self.fallback_func()
        return self.default_value

    def __call__(self, func: Callable) -> Callable:
        """Decorator to wrap a function with fallback."""

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except self.exceptions as e:
                if self.log_errors:
                    resilience_logger.warning(
                        "Fallback triggered for %s: %s", func.__name__, e
                    )
                return self.get_fallback()

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except self.exceptions as e:
                if self.log_errors:
                    resilience_logger.warning(
                        "Fallback triggered for %s: %s", func.__name__, e
                    )
                return self.get_fallback()

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper
