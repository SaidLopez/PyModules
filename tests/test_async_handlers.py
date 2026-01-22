"""
Tests for native async handler support.
"""

import asyncio
from dataclasses import dataclass

import pytest

from pymodules import (
    Event,
    EventInput,
    EventOutput,
    Module,
    ModuleHost,
    ModuleHostConfig,
    module,
)


@dataclass
class AsyncInput(EventInput):
    value: str = ""
    delay: float = 0.0


@dataclass
class AsyncOutput(EventOutput):
    result: str = ""


class AsyncEvent(Event[AsyncInput, AsyncOutput]):
    name = "test.async"


@module(name="AsyncModule")
class AsyncModule(Module):
    """A module with an async handler."""

    def __init__(self):
        super().__init__()
        self.call_count = 0

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, AsyncEvent)

    async def handle(self, event: Event) -> None:
        """Async handler that processes events."""
        if isinstance(event, AsyncEvent):
            self.call_count += 1
            if event.input.delay > 0:
                await asyncio.sleep(event.input.delay)
            event.output = AsyncOutput(result=f"async: {event.input.value}")
            event.handled = True


@module(name="SyncModule")
class SyncModule(Module):
    """A module with a sync handler."""

    def __init__(self):
        super().__init__()
        self.call_count = 0

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, AsyncEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, AsyncEvent):
            self.call_count += 1
            event.output = AsyncOutput(result=f"sync: {event.input.value}")
            event.handled = True


@pytest.mark.asyncio
class TestAsyncHandlers:
    """Tests for native async handler support."""

    async def test_async_handler_via_handle_async(self):
        """Async handlers work with handle_async."""
        host = ModuleHost()
        mod = AsyncModule()
        host.register(mod)

        event = AsyncEvent(input=AsyncInput(value="test"))
        result = await host.handle_async(event)

        assert result.handled
        assert result.output.result == "async: test"
        assert mod.call_count == 1

    async def test_sync_handler_via_handle_async(self):
        """Sync handlers work with handle_async."""
        host = ModuleHost()
        mod = SyncModule()
        host.register(mod)

        event = AsyncEvent(input=AsyncInput(value="test"))
        result = await host.handle_async(event)

        assert result.handled
        assert result.output.result == "sync: test"

    async def test_concurrent_async_events(self):
        """Multiple async events can run concurrently."""
        host = ModuleHost()
        host.register(AsyncModule())

        # Create multiple events with delays
        events = [
            AsyncEvent(input=AsyncInput(value=f"event{i}", delay=0.01))
            for i in range(5)
        ]

        # Dispatch concurrently
        results = await asyncio.gather(*[host.handle_async(e) for e in events])

        assert all(r.handled for r in results)

    async def test_async_with_metrics(self):
        """Async handlers work with metrics."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        event = AsyncEvent(input=AsyncInput(value="test"))
        await host.handle_async(event)

        assert host.metrics.events_dispatched == 1
        assert host.metrics.events_handled == 1

    async def test_async_with_callbacks(self):
        """Async handlers work with event callbacks."""
        started_events = []
        ended_events = []

        config = ModuleHostConfig(
            on_event_start=lambda e: started_events.append(e),
            on_event_end=lambda e, h: ended_events.append((e, h)),
        )
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        event = AsyncEvent(input=AsyncInput(value="test"))
        await host.handle_async(event)

        assert len(started_events) == 1
        assert len(ended_events) == 1
        assert ended_events[0][1] is True  # handled

    def test_async_handler_via_sync_handle(self):
        """Async handlers work with sync handle() too."""
        host = ModuleHost()
        mod = AsyncModule()
        host.register(mod)

        event = AsyncEvent(input=AsyncInput(value="test"))
        result = host.handle(event)

        assert result.handled
        assert result.output.result == "async: test"


@pytest.mark.asyncio
class TestAsyncWithResilience:
    """Tests for async handlers with resilience features."""

    async def test_async_with_rate_limiter(self):
        """Async handlers work with rate limiter."""
        from pymodules import RateLimiter, RateLimitExceeded

        config = ModuleHostConfig(
            rate_limiter=RateLimiter(rate=1, burst=1, block=False)
        )
        host = ModuleHost(config=config)
        host.register(AsyncModule())

        # First should succeed
        event1 = AsyncEvent(input=AsyncInput(value="test1"))
        await host.handle_async(event1)
        assert event1.handled

        # Second should fail
        event2 = AsyncEvent(input=AsyncInput(value="test2"))
        with pytest.raises(RateLimitExceeded):
            await host.handle_async(event2)

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
            def can_handle(self, event: Event) -> bool:
                return isinstance(event, AsyncEvent)

            async def handle(self, event: Event) -> None:
                raise ValueError("Async failure")

        host.register(FailingAsyncModule())

        # Cause failure
        event1 = AsyncEvent(input=AsyncInput(value="test1"))
        await host.handle_async(event1)

        # Circuit should be open
        event2 = AsyncEvent(input=AsyncInput(value="test2"))
        with pytest.raises(CircuitBreakerOpen):
            await host.handle_async(event2)

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
            def can_handle(self, event: Event) -> bool:
                return isinstance(event, AsyncEvent)

            async def handle(self, event: Event) -> None:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("Temporary async failure")
                event.output = AsyncOutput(result="success")
                event.handled = True

        host.register(FlakyAsyncModule())

        event = AsyncEvent(input=AsyncInput(value="test"))
        await host.handle_async(event)

        assert event.handled
        assert call_count == 3
        assert host.metrics.events_retried == 2
