"""
Tests for native async handler support.
"""

import asyncio
from dataclasses import dataclass

import pytest

from pymodules import (
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
    async def handle_async(self, command: AsyncCommand) -> None:
        """Async handler that processes commands."""
        self.call_count += 1
        if command.input.delay > 0:
            await asyncio.sleep(command.input.delay)
        command.output = AsyncOutput(result=f"async: {command.input.value}")
        command.handled = True


@module(name="SyncModule")
class SyncModule(Module):
    """A module with a sync handler."""

    def __init__(self):
        super().__init__()
        self.call_count = 0

    @handles(AsyncCommand)
    def handle_sync(self, command: AsyncCommand) -> None:
        self.call_count += 1
        command.output = AsyncOutput(result=f"sync: {command.input.value}")
        command.handled = True


class TestAsyncHandlers:
    """Tests for native async handler support."""

    @pytest.mark.asyncio
    async def test_async_handler_via_dispatch_async(self):
        """Async handlers work with dispatch_async."""
        host = ModuleHost()
        mod = AsyncModule()
        host.register(mod)

        command = AsyncCommand(input=AsyncInput(value="test"))
        result = await host.dispatch_async(command)

        assert result.handled
        assert result.output.result == "async: test"
        assert mod.call_count == 1

    @pytest.mark.asyncio
    async def test_sync_handler_via_dispatch_async(self):
        """Sync handlers work with dispatch_async."""
        host = ModuleHost()
        mod = SyncModule()
        host.register(mod)

        command = AsyncCommand(input=AsyncInput(value="test"))
        result = await host.dispatch_async(command)

        assert result.handled
        assert result.output.result == "sync: test"

    @pytest.mark.asyncio
    async def test_concurrent_async_commands(self):
        """Multiple async commands can run concurrently."""
        host = ModuleHost()
        host.register(AsyncModule())

        # Create multiple commands with delays
        commands = [
            AsyncCommand(input=AsyncInput(value=f"command{i}", delay=0.01)) for i in range(5)
        ]

        # Dispatch concurrently
        results = await asyncio.gather(*[host.dispatch_async(c) for c in commands])

        assert all(r.handled for r in results)

    @pytest.mark.asyncio
    async def test_async_with_metrics(self):
        """Async handlers work with metrics."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        command = AsyncCommand(input=AsyncInput(value="test"))
        await host.dispatch_async(command)

        assert host.metrics.events_dispatched == 1
        assert host.metrics.events_handled == 1

    @pytest.mark.asyncio
    async def test_async_with_callbacks(self):
        """Async handlers work with lifecycle callbacks."""
        started = []
        ended = []

        config = ModuleHostConfig(
            on_event_start=lambda c: started.append(c),
            on_event_end=lambda c, h: ended.append((c, h)),
        )
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        command = AsyncCommand(input=AsyncInput(value="test"))
        await host.dispatch_async(command)

        assert len(started) == 1
        assert len(ended) == 1
        assert ended[0][1] is True  # handled

    def test_async_handler_via_sync_dispatch(self):
        """Async handlers work with sync dispatch() too."""
        host = ModuleHost()
        mod = AsyncModule()
        host.register(mod)

        command = AsyncCommand(input=AsyncInput(value="test"))
        result = host.dispatch(command)

        assert result.handled
        assert result.output.result == "async: test"


class TestAsyncWithResilience:
    """Tests for async handlers with resilience features."""

    @pytest.mark.asyncio
    async def test_async_with_rate_limiter(self):
        """Async handlers work with rate limiter."""
        from pymodules import RateLimiter, RateLimitExceeded

        config = ModuleHostConfig(rate_limiter=RateLimiter(rate=1, burst=1, block=False))
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        # First should succeed
        command1 = AsyncCommand(input=AsyncInput(value="test1"))
        await host.dispatch_async(command1)
        assert command1.handled

        # Second should fail
        command2 = AsyncCommand(input=AsyncInput(value="test2"))
        with pytest.raises(RateLimitExceeded):
            await host.dispatch_async(command2)

    @pytest.mark.asyncio
    async def test_async_with_circuit_breaker(self):
        """Async handlers work with circuit breaker."""
        from pymodules import CircuitBreaker, CircuitBreakerOpen

        config = ModuleHostConfig(
            circuit_breaker=CircuitBreaker(failure_threshold=1),
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)

        @module(name="FailingAsync")
        class FailingAsyncModule(Module):
            @handles(AsyncCommand)
            async def fail(self, command: AsyncCommand) -> None:
                raise ValueError("Async failure")

        host.register(FailingAsyncModule())

        # Cause failure
        command1 = AsyncCommand(input=AsyncInput(value="test1"))
        await host.dispatch_async(command1)

        # Circuit should be open
        command2 = AsyncCommand(input=AsyncInput(value="test2"))
        with pytest.raises(CircuitBreakerOpen):
            await host.dispatch_async(command2)

    @pytest.mark.asyncio
    async def test_async_with_retry(self):
        """Async handlers work with retry policy."""
        from pymodules import RetryPolicy

        config = ModuleHostConfig(
            retry_policy=RetryPolicy(max_retries=2, base_delay=0.01),
            propagate_exceptions=False,
            enable_metrics=True,
        )
        host = ModuleHost(config=config)

        call_count = 0

        @module(name="FlakyAsync")
        class FlakyAsyncModule(Module):
            @handles(AsyncCommand)
            async def flaky(self, command: AsyncCommand) -> None:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("Temporary async failure")
                command.output = AsyncOutput(result="success")
                command.handled = True

        host.register(FlakyAsyncModule())

        command = AsyncCommand(input=AsyncInput(value="test"))
        await host.dispatch_async(command)

        assert command.handled
        assert call_count == 3
        assert host.metrics.events_retried == 2


class TestAsyncLoopReuse:
    """Tests for asyncio loop management in async handlers."""

    @pytest.mark.asyncio
    async def test_async_dispatch_reuses_asyncio_loop(self):
        """Verify dispatch_async reuses the same asyncio loop for async handlers."""
        loop_ids = []

        class TrackCommand(Command[CommandRequest, CommandResponse]):
            name = "track"

        @module(name="LoopTracker")
        class LoopTrackerModule(Module):
            @handles(TrackCommand)
            async def track(self, command: TrackCommand) -> None:
                loop_ids.append(id(asyncio.get_running_loop()))
                command.handled = True

        host = ModuleHost()
        host.register(LoopTrackerModule())

        command1 = TrackCommand(input=CommandRequest())
        command2 = TrackCommand(input=CommandRequest())

        await host.dispatch_async(command1)
        await host.dispatch_async(command2)

        assert len(loop_ids) == 2, "Should have tracked two loop IDs"
        assert len(set(loop_ids)) == 1, "Should reuse the same event loop"

    def test_sync_dispatch_with_async_handler_uses_asyncio_run(self):
        """Verify sync dispatch uses asyncio.run for async handlers (no nested loops)."""
        call_count = 0

        class CountCommand(Command[CommandRequest, CommandResponse]):
            name = "count"

        @module(name="AsyncCounter")
        class AsyncCounterModule(Module):
            @handles(CountCommand)
            async def count(self, command: CountCommand) -> None:
                nonlocal call_count
                call_count += 1
                command.handled = True

        host = ModuleHost()
        host.register(AsyncCounterModule())

        # Multiple sync calls with async handlers should work without issues
        command1 = CountCommand(input=CommandRequest())
        command2 = CountCommand(input=CommandRequest())

        host.dispatch(command1)
        host.dispatch(command2)

        assert call_count == 2, "Both commands should be handled"
        assert command1.handled
        assert command2.handled
