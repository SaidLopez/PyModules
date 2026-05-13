"""
Tests for ``RetryPolicy`` and ``RetryMiddleware``.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    ModuleHostConfig,
    RetryMiddleware,
    RetryPolicy,
    handles,
    module,
)


@dataclass
class RetryInput(CommandRequest):
    should_fail: bool = False


@dataclass
class RetryOutput(CommandResponse):
    result: str = ""


class RetryCommand(Command[RetryInput, RetryOutput]):
    name = "test.retry"


@module(name="RetryFailingModule")
class RetryFailingModule(Module):
    def __init__(self):
        super().__init__()
        self.call_count = 0

    @handles(RetryCommand)
    def handle(self, command: RetryCommand) -> RetryOutput:
        self.call_count += 1
        if command.request.should_fail:
            raise ValueError("Intentional failure")
        return RetryOutput(result="ok")


class TestRetryPolicy:
    def test_calculates_delay(self):
        policy = RetryPolicy(base_delay=1.0, exponential_base=2.0, max_delay=60)
        assert policy.calculate_delay(0) == 1.0
        assert policy.calculate_delay(1) == 2.0
        assert policy.calculate_delay(2) == 4.0

    def test_respects_max_delay(self):
        policy = RetryPolicy(base_delay=1.0, exponential_base=2.0, max_delay=5.0)
        assert policy.calculate_delay(10) == 5.0

    def test_should_retry(self):
        policy = RetryPolicy(max_retries=3, retryable_exceptions=(ValueError,))
        assert policy.should_retry(ValueError(), 0) is True
        assert policy.should_retry(ValueError(), 3) is False
        assert policy.should_retry(TypeError(), 0) is False

    def test_decorator_retries(self):
        policy = RetryPolicy(max_retries=3, base_delay=0.01)
        call_count = 0

        @policy
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary")
            return "success"

        assert flaky() == "success"
        assert call_count == 3

    def test_decorator_gives_up(self):
        policy = RetryPolicy(max_retries=2, base_delay=0.01)

        @policy
        def always_fails():
            raise ValueError("Permanent")

        with pytest.raises(ValueError):
            always_fails()


class TestRetryMiddleware:
    def test_integration_retries_on_failure(self):
        mw = RetryMiddleware(RetryPolicy(max_retries=2, base_delay=0.01))
        config = ModuleHostConfig(
            middleware=[mw],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)
        mod = RetryFailingModule()
        host.register(mod)

        host.dispatch(RetryCommand(request=RetryInput(should_fail=True)))

        # 1 initial attempt + 2 retries = 3 calls
        assert mod.call_count == 3
        assert mw.retry_count == 2


class TestRetryRespectsFrameworkSignals:
    """``PyModulesSignal`` subclasses must NOT be retried."""

    def test_should_retry_returns_false_for_signal(self):
        from pymodules import CircuitBreakerOpen, RateLimitExceeded, UnknownCommandError

        policy = RetryPolicy(max_retries=5)  # retryable_exceptions defaults to (Exception,)
        assert policy.should_retry(CircuitBreakerOpen("open"), 0) is False
        assert policy.should_retry(RateLimitExceeded("rl"), 0) is False
        assert policy.should_retry(UnknownCommandError(RetryCommand), 0) is False
        # Sanity: real handler errors still retry.
        assert policy.should_retry(ValueError("boom"), 0) is True

    def test_middleware_does_not_retry_unknown_command(self):
        from pymodules import UnknownCommandError

        mw = RetryMiddleware(RetryPolicy(max_retries=5, base_delay=0.01))
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        # No module registered — terminal raises UnknownCommandError.
        with pytest.raises(UnknownCommandError):
            host.dispatch(RetryCommand(request=RetryInput(should_fail=False)))
        # Critically: no retries.
        assert mw.retry_count == 0
