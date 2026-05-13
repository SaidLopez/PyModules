"""
Tests for ``RateLimitMiddleware`` and integration with ``ModuleHost``.
"""

import time
from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    ModuleHostConfig,
    RateLimitExceeded,
    RateLimitMiddleware,
    handles,
    module,
)


@dataclass
class RLInput(CommandRequest):
    value: str = ""


@dataclass
class RLOutput(CommandResponse):
    result: str = ""


class RLCommand(Command[RLInput, RLOutput]):
    name = "test.ratelimit"


@module(name="RLModule")
class RLModule(Module):
    @handles(RLCommand)
    def handle(self, command: RLCommand) -> RLOutput:
        return RLOutput(result=f"processed: {command.request.value}")


class TestRateLimitMiddleware:
    """Direct middleware unit tests."""

    def test_starts_full(self):
        mw = RateLimitMiddleware(rate=100, burst=10)
        assert mw.rejected_count == 0
        # tokens initialised to burst
        assert mw._tokens == 10.0

    def test_reset(self):
        mw = RateLimitMiddleware(rate=1, burst=1, block=False)
        mw._tokens = 0
        mw.reset()
        assert mw._tokens == 1.0


class TestRateLimitWiring:
    """Integration: middleware in the host's chain."""

    def test_allows_within_burst(self):
        mw = RateLimitMiddleware(rate=100, burst=10)
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(RLModule())

        for _ in range(5):
            response = host.dispatch(RLCommand(request=RLInput(value="x")))
            assert response is not None
        assert mw.rejected_count == 0

    def test_rejects_when_exceeded(self):
        mw = RateLimitMiddleware(rate=1, burst=1, block=False)
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(RLModule())

        response = host.dispatch(RLCommand(request=RLInput(value="first")))
        assert response is not None

        with pytest.raises(RateLimitExceeded):
            host.dispatch(RLCommand(request=RLInput(value="second")))

        assert mw.rejected_count == 1

    def test_blocking_mode(self):
        # block=True waits for tokens to refill instead of raising.
        mw = RateLimitMiddleware(rate=100, burst=1, block=True)
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(RLModule())

        start = time.monotonic()
        host.dispatch(RLCommand(request=RLInput(value="a")))
        host.dispatch(RLCommand(request=RLInput(value="b")))
        elapsed = time.monotonic() - start
        # Second dispatch should have waited for the bucket to refill.
        assert elapsed >= 0.005

    @pytest.mark.asyncio
    async def test_async_rate_limit(self):
        mw = RateLimitMiddleware(rate=1, burst=1, block=False)
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(RLModule())

        await host.dispatch_async(RLCommand(request=RLInput(value="1")))
        with pytest.raises(RateLimitExceeded):
            await host.dispatch_async(RLCommand(request=RLInput(value="2")))
