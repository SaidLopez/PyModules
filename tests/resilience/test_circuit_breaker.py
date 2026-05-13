"""
Tests for ``CircuitBreaker`` and ``CircuitBreakerMiddleware``.
"""

import time
from dataclasses import dataclass

import pytest

from pymodules import (
    CircuitBreaker,
    CircuitBreakerMiddleware,
    CircuitBreakerOpen,
    CircuitState,
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    ModuleHostConfig,
    handles,
    module,
)


@dataclass
class CBInput(CommandRequest):
    should_fail: bool = False


@dataclass
class CBOutput(CommandResponse):
    result: str = ""


class CBCommand(Command[CBInput, CBOutput]):
    name = "test.cb"


@module(name="CBFailingModule")
class CBFailingModule(Module):
    def __init__(self):
        super().__init__()
        self.call_count = 0

    @handles(CBCommand)
    def handle(self, command: CBCommand) -> CBOutput:
        self.call_count += 1
        if command.request.should_fail:
            raise ValueError("Intentional failure")
        return CBOutput(result="ok")


class TestCircuitBreaker:
    """Tests for the state machine itself."""

    def test_starts_closed(self):
        breaker = CircuitBreaker(failure_threshold=3)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.allow_request() is True

    def test_opens_after_failures(self):
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert breaker.allow_request() is False

    def test_half_open_after_timeout(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        time.sleep(0.15)
        assert breaker.state == CircuitState.HALF_OPEN

    def test_closes_after_success_in_half_open(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, success_threshold=2)
        breaker.record_failure()
        time.sleep(0.15)
        assert breaker.state == CircuitState.HALF_OPEN
        breaker.record_success()
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        breaker.record_failure()
        time.sleep(0.15)
        assert breaker.state == CircuitState.HALF_OPEN
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    def test_decorator(self):
        breaker = CircuitBreaker(failure_threshold=2)

        call_count = 0

        @breaker
        def flaky():
            nonlocal call_count
            call_count += 1
            raise ValueError("Failed")

        for _ in range(2):
            with pytest.raises(ValueError):
                flaky()

        with pytest.raises(CircuitBreakerOpen):
            flaky()

        assert call_count == 2


class TestCircuitBreakerMiddleware:
    """Tests for the middleware adapter."""

    def test_integration_opens_after_failures(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        mw = CircuitBreakerMiddleware(breaker)
        config = ModuleHostConfig(
            middleware=[mw],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)
        host.register(CBFailingModule())

        for _ in range(2):
            host.dispatch(CBCommand(request=CBInput(should_fail=True)))

        with pytest.raises(CircuitBreakerOpen):
            host.dispatch(CBCommand(request=CBInput()))

        assert mw.rejected_count == 1

    def test_breaker_is_observable(self):
        breaker = CircuitBreaker(failure_threshold=1)
        mw = CircuitBreakerMiddleware(breaker)
        assert mw.breaker is breaker
