"""
Tests for resilience patterns: rate limiting, circuit breaker, retry, DLQ.
"""

import time
from dataclasses import dataclass

import pytest

from pymodules import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    DeadLetterQueue,
    Event,
    EventInput,
    EventOutput,
    Fallback,
    Module,
    ModuleHost,
    ModuleHostConfig,
    RateLimiter,
    RateLimitExceeded,
    RetryPolicy,
    module,
)


@dataclass
class TestInput(EventInput):
    value: str = ""
    should_fail: bool = False


@dataclass
class TestOutput(EventOutput):
    result: str = ""


class TestEvent(Event[TestInput, TestOutput]):
    name = "test.resilience"


@module(name="FailingModule")
class FailingModule(Module):
    def __init__(self):
        super().__init__()
        self.call_count = 0

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, TestEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, TestEvent):
            self.call_count += 1
            if event.input.should_fail:
                raise ValueError("Intentional failure")
            event.output = TestOutput(result=f"processed: {event.input.value}")
            event.handled = True


class TestRateLimiter:
    """Tests for rate limiter."""

    def test_allows_within_limit(self):
        """Rate limiter allows requests within limit."""
        limiter = RateLimiter(rate=100, burst=10)

        # Should allow 10 requests (burst)
        for _ in range(10):
            assert limiter.acquire() is True

    def test_raises_when_exceeded(self):
        """Rate limiter raises when limit exceeded."""
        limiter = RateLimiter(rate=1, burst=1, block=False)

        # First should succeed
        assert limiter.acquire() is True

        # Second should fail immediately
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.acquire()

        assert exc_info.value.retry_after > 0

    def test_blocking_mode(self):
        """Rate limiter blocks when in blocking mode."""
        limiter = RateLimiter(rate=100, burst=1, block=True)

        start = time.monotonic()
        limiter.acquire()
        limiter.acquire()  # Should block briefly
        elapsed = time.monotonic() - start

        # Should have waited some time
        assert elapsed >= 0.005  # At least 5ms

    def test_reset(self):
        """Rate limiter can be reset."""
        limiter = RateLimiter(rate=1, burst=1, block=False)

        limiter.acquire()

        with pytest.raises(RateLimitExceeded):
            limiter.acquire()

        limiter.reset()

        # Should succeed again
        assert limiter.acquire() is True


class TestCircuitBreaker:
    """Tests for circuit breaker."""

    def test_starts_closed(self):
        """Circuit breaker starts in closed state."""
        breaker = CircuitBreaker(failure_threshold=3)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.allow_request() is True

    def test_opens_after_failures(self):
        """Circuit breaker opens after threshold failures."""
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10)

        # Record failures
        for _ in range(3):
            breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert breaker.allow_request() is False

    def test_half_open_after_timeout(self):
        """Circuit breaker transitions to half-open after timeout."""
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)

        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Wait for timeout
        time.sleep(0.15)

        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.allow_request() is True

    def test_closes_after_success_in_half_open(self):
        """Circuit breaker closes after successes in half-open."""
        breaker = CircuitBreaker(
            failure_threshold=1, recovery_timeout=0.1, success_threshold=2
        )

        breaker.record_failure()
        time.sleep(0.15)

        assert breaker.state == CircuitState.HALF_OPEN

        breaker.record_success()
        breaker.record_success()

        assert breaker.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        """Circuit breaker reopens on failure in half-open."""
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)

        breaker.record_failure()
        time.sleep(0.15)

        assert breaker.state == CircuitState.HALF_OPEN

        breaker.record_failure()

        assert breaker.state == CircuitState.OPEN

    def test_decorator(self):
        """Circuit breaker works as decorator."""
        breaker = CircuitBreaker(failure_threshold=2)

        call_count = 0

        @breaker
        def flaky_function():
            nonlocal call_count
            call_count += 1
            raise ValueError("Failed")

        # First two failures
        for _ in range(2):
            with pytest.raises(ValueError):
                flaky_function()

        # Circuit should be open now
        with pytest.raises(CircuitBreakerOpen):
            flaky_function()

        assert call_count == 2  # Third call never executed


class TestRetryPolicy:
    """Tests for retry policy."""

    def test_calculates_delay(self):
        """Retry policy calculates exponential delay."""
        policy = RetryPolicy(base_delay=1.0, exponential_base=2.0, max_delay=60)

        assert policy.calculate_delay(0) == 1.0
        assert policy.calculate_delay(1) == 2.0
        assert policy.calculate_delay(2) == 4.0
        assert policy.calculate_delay(3) == 8.0

    def test_respects_max_delay(self):
        """Retry policy respects maximum delay."""
        policy = RetryPolicy(base_delay=1.0, exponential_base=2.0, max_delay=5.0)

        assert policy.calculate_delay(10) == 5.0  # Would be 1024 without max

    def test_should_retry(self):
        """Retry policy determines if should retry."""
        policy = RetryPolicy(max_retries=3, retryable_exceptions=(ValueError,))

        assert policy.should_retry(ValueError(), 0) is True
        assert policy.should_retry(ValueError(), 2) is True
        assert policy.should_retry(ValueError(), 3) is False  # Max reached
        assert policy.should_retry(TypeError(), 0) is False  # Wrong exception type

    def test_decorator_retries(self):
        """Retry policy decorator retries on failure."""
        policy = RetryPolicy(max_retries=3, base_delay=0.01)

        call_count = 0

        @policy
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary failure")
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 3

    def test_decorator_gives_up(self):
        """Retry policy decorator gives up after max retries."""
        policy = RetryPolicy(max_retries=2, base_delay=0.01)

        @policy
        def always_fails():
            raise ValueError("Permanent failure")

        with pytest.raises(ValueError):
            always_fails()


class TestDeadLetterQueue:
    """Tests for dead letter queue."""

    def test_add_entry(self):
        """DLQ accepts entries."""
        dlq = DeadLetterQueue(max_size=100)
        event = TestEvent(input=TestInput(value="test"))
        error = ValueError("Test error")

        entry = dlq.add(event, error, module_name="TestModule")

        assert len(dlq) == 1
        assert entry.event is event
        assert entry.error is error
        assert entry.module_name == "TestModule"

    def test_max_size(self):
        """DLQ respects max size."""
        dlq = DeadLetterQueue(max_size=2)

        for i in range(5):
            event = TestEvent(input=TestInput(value=str(i)))
            dlq.add(event, ValueError(f"Error {i}"))

        assert len(dlq) == 2
        # Oldest entries should be dropped
        entries = dlq.entries
        assert entries[0].event.input.value == "3"
        assert entries[1].event.input.value == "4"

    def test_pop(self):
        """DLQ supports pop."""
        dlq = DeadLetterQueue(max_size=10)

        event1 = TestEvent(input=TestInput(value="first"))
        event2 = TestEvent(input=TestInput(value="second"))

        dlq.add(event1, ValueError("Error 1"))
        dlq.add(event2, ValueError("Error 2"))

        entry = dlq.pop()
        assert entry.event.input.value == "first"
        assert len(dlq) == 1

    def test_clear(self):
        """DLQ can be cleared."""
        dlq = DeadLetterQueue(max_size=10)

        for i in range(5):
            dlq.add(TestEvent(input=TestInput(value=str(i))), ValueError(""))

        count = dlq.clear()
        assert count == 5
        assert len(dlq) == 0

    def test_on_add_callback(self):
        """DLQ calls on_add callback."""
        added_entries = []

        def on_add(entry):
            added_entries.append(entry)

        dlq = DeadLetterQueue(max_size=10, on_add=on_add)
        event = TestEvent(input=TestInput(value="test"))
        dlq.add(event, ValueError("Error"))

        assert len(added_entries) == 1
        assert added_entries[0].event is event


class TestFallback:
    """Tests for fallback handler."""

    def test_returns_default_value(self):
        """Fallback returns default value on error."""
        fallback = Fallback(default_value="fallback_value", log_errors=False)

        @fallback
        def failing_function():
            raise ValueError("Failed")

        result = failing_function()
        assert result == "fallback_value"

    def test_returns_normal_value(self):
        """Fallback returns normal value when no error."""
        fallback = Fallback(default_value="fallback_value")

        @fallback
        def working_function():
            return "normal_value"

        result = working_function()
        assert result == "normal_value"

    def test_fallback_function(self):
        """Fallback can use a function."""
        fallback = Fallback(fallback_func=lambda: {"status": "degraded"}, log_errors=False)

        @fallback
        def failing_function():
            raise ValueError("Failed")

        result = failing_function()
        assert result == {"status": "degraded"}

    def test_specific_exceptions(self):
        """Fallback only catches specified exceptions."""
        fallback = Fallback(
            default_value="fallback", exceptions=(ValueError,), log_errors=False
        )

        @fallback
        def raises_value_error():
            raise ValueError("Caught")

        @fallback
        def raises_type_error():
            raise TypeError("Not caught")

        assert raises_value_error() == "fallback"

        with pytest.raises(TypeError):
            raises_type_error()


class TestHostWithResilience:
    """Tests for ModuleHost with resilience features."""

    def test_rate_limiting_integration(self):
        """ModuleHost respects rate limiter."""
        config = ModuleHostConfig(
            rate_limiter=RateLimiter(rate=1, burst=1, block=False),
            enable_metrics=True,
        )
        host = ModuleHost(config=config)
        host.register(FailingModule())

        # First event should succeed
        event1 = TestEvent(input=TestInput(value="test"))
        host.handle(event1)
        assert event1.handled

        # Second event should be rate limited
        event2 = TestEvent(input=TestInput(value="test2"))
        with pytest.raises(RateLimitExceeded):
            host.handle(event2)

        assert host.metrics.events_rate_limited == 1

    def test_circuit_breaker_integration(self):
        """ModuleHost respects circuit breaker."""
        config = ModuleHostConfig(
            circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_timeout=10),
            propagate_exceptions=False,
            enable_metrics=True,
        )
        host = ModuleHost(config=config)
        host.register(FailingModule())

        # Cause failures to open circuit
        for _ in range(2):
            event = TestEvent(input=TestInput(should_fail=True))
            host.handle(event)

        # Next request should be rejected by circuit breaker
        event = TestEvent(input=TestInput(value="test"))
        with pytest.raises(CircuitBreakerOpen):
            host.handle(event)

        assert host.metrics.events_circuit_broken == 1

    def test_dlq_integration(self):
        """ModuleHost sends failed events to DLQ."""
        dlq = DeadLetterQueue(max_size=100)
        config = ModuleHostConfig(
            dead_letter_queue=dlq,
            propagate_exceptions=False,
            enable_metrics=True,
        )
        host = ModuleHost(config=config)
        host.register(FailingModule())

        event = TestEvent(input=TestInput(should_fail=True))
        host.handle(event)

        assert len(dlq) == 1
        assert host.metrics.events_dead_lettered == 1

    def test_retry_integration(self):
        """ModuleHost retries with retry policy."""
        config = ModuleHostConfig(
            retry_policy=RetryPolicy(max_retries=2, base_delay=0.01),
            propagate_exceptions=False,
            enable_metrics=True,
        )
        host = ModuleHost(config=config)
        mod = FailingModule()
        host.register(mod)

        event = TestEvent(input=TestInput(should_fail=True))
        host.handle(event)

        # Should have been called 3 times (1 + 2 retries)
        assert mod.call_count == 3
        assert host.metrics.events_retried == 2
