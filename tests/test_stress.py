"""
Stress tests for PyModules.

These tests verify behavior under load and concurrent access.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
class StressInput(EventInput):
    value: int = 0


@dataclass
class StressOutput(EventOutput):
    result: int = 0


class StressEvent(Event[StressInput, StressOutput]):
    name = "test.stress"


@module(name="StressModule")
class StressModule(Module):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self._lock = threading.Lock()

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, StressEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, StressEvent):
            with self._lock:
                self.call_count += 1
            # Simulate some work
            result = event.input.value * 2
            event.output = StressOutput(result=result)
            event.handled = True


@module(name="SlowModule")
class SlowModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, StressEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, StressEvent):
            time.sleep(0.01)  # 10ms delay
            event.output = StressOutput(result=event.input.value)
            event.handled = True


@pytest.mark.slow
class TestConcurrentDispatch:
    """Tests for concurrent event dispatch."""

    def test_many_sequential_events(self):
        """Test handling many events sequentially."""
        host = ModuleHost()
        mod = StressModule()
        host.register(mod)

        num_events = 1000
        for i in range(num_events):
            event = StressEvent(input=StressInput(value=i))
            host.handle(event)
            assert event.handled
            assert event.output.result == i * 2

        assert mod.call_count == num_events

    def test_concurrent_events_with_thread_pool(self):
        """Test handling events from multiple threads."""
        config = ModuleHostConfig(max_workers=8)
        host = ModuleHost(config=config)
        mod = StressModule()
        host.register(mod)

        num_events = 100
        results = []

        def dispatch_event(value):
            event = StressEvent(input=StressInput(value=value))
            host.handle(event)
            return event

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(dispatch_event, i) for i in range(num_events)]
            for future in as_completed(futures):
                results.append(future.result())

        assert len(results) == num_events
        assert all(e.handled for e in results)
        assert mod.call_count == num_events

    def test_async_events_concurrent(self):
        """Test async handling of concurrent events."""
        import asyncio

        config = ModuleHostConfig(max_workers=8)
        host = ModuleHost(config=config)
        host.register(StressModule())

        async def dispatch_many():
            tasks = []
            for i in range(50):
                event = StressEvent(input=StressInput(value=i))
                tasks.append(host.handle_async(event))

            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(dispatch_many())
        assert len(results) == 50
        assert all(e.handled for e in results)


@pytest.mark.slow
class TestManyModules:
    """Tests with many registered modules."""

    def test_many_modules_performance(self):
        """Test performance with many registered modules."""
        host = ModuleHost()

        # Register 100 modules, only the last one handles StressEvent
        for i in range(99):

            @module(name=f"DummyModule{i}")
            class DummyModule(Module):
                def can_handle(self, event: Event) -> bool:
                    return False

                def handle(self, event: Event) -> None:
                    pass

            host.register(DummyModule())

        host.register(StressModule())

        # Dispatch events
        start = time.time()
        num_events = 100
        for i in range(num_events):
            event = StressEvent(input=StressInput(value=i))
            host.handle(event)
            assert event.handled

        elapsed = time.time() - start
        # Should complete in reasonable time (< 1 second)
        assert elapsed < 1.0


@pytest.mark.slow
class TestResourceCleanup:
    """Tests for resource management."""

    def test_shutdown_waits_for_tasks(self):
        """Test that shutdown waits for in-flight tasks."""
        config = ModuleHostConfig(max_workers=2)
        host = ModuleHost(config=config)
        host.register(SlowModule())

        # Start some async tasks
        import asyncio

        async def run_and_shutdown():
            # Start events
            tasks = [host.handle_async(StressEvent(input=StressInput(value=i))) for i in range(5)]
            # Wait for all
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(run_and_shutdown())
        assert all(e.handled for e in results)

        # Shutdown should complete without error
        host.shutdown(wait=True)

    def test_events_in_progress_tracking(self):
        """Test that events_in_progress is properly maintained."""
        host = ModuleHost()
        tracking = []

        @module(name="TrackingModule")
        class TrackingModule(Module):
            def can_handle(self, event: Event) -> bool:
                return isinstance(event, StressEvent)

            def handle(self, event: Event) -> None:
                # Check events_in_progress during handling
                tracking.append(len(host.events_in_progress))
                event.handled = True

        host.register(TrackingModule())

        for i in range(10):
            event = StressEvent(input=StressInput(value=i))
            host.handle(event)

        # During each handle, there should be exactly 1 event in progress
        assert all(count == 1 for count in tracking)
        # After all events, should be empty
        assert len(host.events_in_progress) == 0
