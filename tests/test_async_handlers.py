"""
Tests for native async handler support.
"""

import asyncio
from dataclasses import dataclass

import pytest

from pymodules import (
    CircuitBreaker,
    CircuitBreakerMiddleware,
    CircuitBreakerOpen,
    Command,
    CommandRequest,
    CommandResponse,
    LifecycleMiddleware,
    MetricsMiddleware,
    Module,
    ModuleHost,
    ModuleHostConfig,
    RateLimitExceeded,
    RateLimitMiddleware,
    RetryMiddleware,
    RetryPolicy,
    SyncDispatchOnAsyncHandlerError,
    handles,
    module,
)


@dataclass
class AsyncInput(CommandRequest):
    value: str = ""
    delay: float = 0.0


@dataclass
class AsyncOutput(CommandResponse):
    result: str = ""


class AsyncCommand(Command[AsyncInput, AsyncOutput]):
    name = "test.async"


@module(name="AsyncModule")
class AsyncModule(Module):
    """A module with an async handler."""

    def __init__(self):
        super().__init__()
        self.call_count = 0

    @handles(AsyncCommand)
    async def handle_async(self, command: AsyncCommand) -> AsyncOutput:
        """Async handler that processes commands."""
        self.call_count += 1
        if command.request.delay > 0:
            await asyncio.sleep(command.request.delay)
        return AsyncOutput(result=f"async: {command.request.value}")


@module(name="SyncModule")
class SyncModule(Module):
    """A module with a sync handler."""

    def __init__(self):
        super().__init__()
        self.call_count = 0

    @handles(AsyncCommand)
    def handle_sync(self, command: AsyncCommand) -> AsyncOutput:
        self.call_count += 1
        return AsyncOutput(result=f"sync: {command.request.value}")


class TestAsyncHandlers:
    """Tests for native async handler support."""

    @pytest.mark.asyncio
    async def test_async_handler_via_dispatch_async(self):
        host = ModuleHost()
        mod = AsyncModule()
        host.register(mod)

        command = AsyncCommand(request=AsyncInput(value="test"))
        response = await host.dispatch_async(command)

        assert response.result == "async: test"
        assert mod.call_count == 1

    @pytest.mark.asyncio
    async def test_sync_handler_via_dispatch_async(self):
        host = ModuleHost()
        mod = SyncModule()
        host.register(mod)

        command = AsyncCommand(request=AsyncInput(value="test"))
        response = await host.dispatch_async(command)

        assert response.result == "sync: test"

    @pytest.mark.asyncio
    async def test_concurrent_async_commands(self):
        host = ModuleHost()
        host.register(AsyncModule())

        commands = [
            AsyncCommand(request=AsyncInput(value=f"command{i}", delay=0.01)) for i in range(5)
        ]
        results = await asyncio.gather(*[host.dispatch_async(c) for c in commands])
        assert all(isinstance(r, AsyncOutput) for r in results)

    @pytest.mark.asyncio
    async def test_async_with_metrics(self):
        metrics = MetricsMiddleware()
        host = ModuleHost(config=ModuleHostConfig(middleware=[metrics]))
        host.register(AsyncModule())

        await host.dispatch_async(AsyncCommand(request=AsyncInput(value="test")))

        assert metrics.dispatched == 1
        assert metrics.succeeded == 1

    @pytest.mark.asyncio
    async def test_async_with_lifecycle_callbacks(self):
        started = []
        ended = []
        lifecycle = LifecycleMiddleware(
            on_start=started.append,
            on_end=lambda c, h: ended.append((c, h)),
        )
        host = ModuleHost(config=ModuleHostConfig(middleware=[lifecycle]))
        host.register(AsyncModule())

        await host.dispatch_async(AsyncCommand(request=AsyncInput(value="test")))

        assert len(started) == 1
        assert len(ended) == 1
        assert ended[0][1] is True

    def test_sync_dispatch_on_async_handler_raises(self):
        """Sync dispatch() on an async handler raises — does not bridge."""
        host = ModuleHost()
        host.register(AsyncModule())

        with pytest.raises(SyncDispatchOnAsyncHandlerError):
            host.dispatch(AsyncCommand(request=AsyncInput(value="test")))


class TestAsyncWithResilience:
    """Tests for async handlers with resilience features."""

    @pytest.mark.asyncio
    async def test_async_with_rate_limiter(self):
        config = ModuleHostConfig(
            middleware=[RateLimitMiddleware(rate=1, burst=1, block=False)],
        )
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        # First should succeed.
        response = await host.dispatch_async(AsyncCommand(request=AsyncInput(value="1")))
        assert response is not None

        with pytest.raises(RateLimitExceeded):
            await host.dispatch_async(AsyncCommand(request=AsyncInput(value="2")))

    @pytest.mark.asyncio
    async def test_async_with_circuit_breaker(self):
        breaker = CircuitBreaker(failure_threshold=1)
        config = ModuleHostConfig(
            middleware=[CircuitBreakerMiddleware(breaker)],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)

        @module(name="FailingAsync")
        class FailingAsyncModule(Module):
            @handles(AsyncCommand)
            async def fail(self, command: AsyncCommand) -> AsyncOutput:
                raise ValueError("Async failure")

        host.register(FailingAsyncModule())

        # First call: breaker records failure → open.
        await host.dispatch_async(AsyncCommand(request=AsyncInput(value="1")))

        with pytest.raises(CircuitBreakerOpen):
            await host.dispatch_async(AsyncCommand(request=AsyncInput(value="2")))

    @pytest.mark.asyncio
    async def test_async_with_retry(self):
        retry = RetryMiddleware(RetryPolicy(max_retries=2, base_delay=0.01))
        metrics = MetricsMiddleware()
        config = ModuleHostConfig(
            middleware=[retry, metrics],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)

        call_count = 0

        @module(name="FlakyAsync")
        class FlakyAsyncModule(Module):
            @handles(AsyncCommand)
            async def flaky(self, command: AsyncCommand) -> AsyncOutput:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("Temporary async failure")
                return AsyncOutput(result="success")

        host.register(FlakyAsyncModule())

        response = await host.dispatch_async(AsyncCommand(request=AsyncInput(value="test")))

        assert response is not None
        assert call_count == 3
        assert retry.retry_count == 2


class TestAsyncLoopReuse:
    """Tests for asyncio loop management in async handlers."""

    @pytest.mark.asyncio
    async def test_async_dispatch_reuses_asyncio_loop(self):
        loop_ids = []

        class TrackCommand(Command[CommandRequest, CommandResponse]):
            name = "track"

        @module(name="LoopTracker")
        class LoopTrackerModule(Module):
            @handles(TrackCommand)
            async def track(self, command: TrackCommand) -> CommandResponse:
                loop_ids.append(id(asyncio.get_running_loop()))
                return CommandResponse()

        host = ModuleHost()
        host.register(LoopTrackerModule())

        await host.dispatch_async(TrackCommand(request=CommandRequest()))
        await host.dispatch_async(TrackCommand(request=CommandRequest()))

        assert len(loop_ids) == 2
        assert len(set(loop_ids)) == 1, "Should reuse the same event loop"

    def test_sync_dispatch_runs_sync_handler(self):
        """Sync ``dispatch()`` runs a sync handler via ``asyncio.run``."""
        call_count = 0

        class CountCommand(Command[CommandRequest, CommandResponse]):
            name = "count"

        @module(name="SyncCounter")
        class SyncCounterModule(Module):
            @handles(CountCommand)
            def count(self, command: CountCommand) -> CommandResponse:
                nonlocal call_count
                call_count += 1
                return CommandResponse()

        host = ModuleHost()
        host.register(SyncCounterModule())

        response1 = host.dispatch(CountCommand(request=CommandRequest()))
        response2 = host.dispatch(CountCommand(request=CommandRequest()))

        assert call_count == 2
        assert response1 is not None
        assert response2 is not None
