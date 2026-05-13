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
class StressInput(CommandRequest):
    value: int = 0


@dataclass
class StressOutput(CommandResponse):
    result: int = 0


class StressCommand(Command[StressInput, StressOutput]):
    name = "test.stress"


@module(name="StressModule")
class StressModule(Module):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self._lock = threading.Lock()

    @handles(StressCommand)
    def handle_stress(self, command: StressCommand) -> StressOutput:
        with self._lock:
            self.call_count += 1
        # Simulate some work
        result = command.request.value * 2
        return StressOutput(result=result)


@module(name="SlowModule")
class SlowModule(Module):
    @handles(StressCommand)
    def handle_stress(self, command: StressCommand) -> StressOutput:
        time.sleep(0.01)  # 10ms delay
        return StressOutput(result=command.request.value)


@pytest.mark.slow
class TestConcurrentDispatch:
    """Tests for concurrent command dispatch."""

    def test_many_sequential_commands(self):
        """Test handling many commands sequentially."""
        host = ModuleHost()
        mod = StressModule()
        host.register(mod)

        num_commands = 1000
        for i in range(num_commands):
            command = StressCommand(request=StressInput(value=i))
            response = host.dispatch(command)
            assert response is not None
            assert response.result == i * 2

        assert mod.call_count == num_commands

    def test_concurrent_commands_with_thread_pool(self):
        """Test handling commands from multiple threads."""
        config = ModuleHostConfig(max_workers=8)
        host = ModuleHost(config=config)
        mod = StressModule()
        host.register(mod)

        num_commands = 100
        results = []

        def dispatch_command(value):
            command = StressCommand(request=StressInput(value=value))
            return host.dispatch(command)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(dispatch_command, i) for i in range(num_commands)]
            for future in as_completed(futures):
                results.append(future.result())

        assert len(results) == num_commands
        assert all(r is not None for r in results)
        assert mod.call_count == num_commands

    def test_async_commands_concurrent(self):
        """Test async handling of concurrent commands."""
        import asyncio

        config = ModuleHostConfig(max_workers=8)
        host = ModuleHost(config=config)
        host.register(StressModule())

        async def dispatch_many():
            tasks = []
            for i in range(50):
                command = StressCommand(request=StressInput(value=i))
                tasks.append(host.dispatch_async(command))

            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(dispatch_many())
        assert len(results) == 50
        assert all(r is not None for r in results)


@pytest.mark.slow
class TestManyModules:
    """Tests with many registered modules."""

    def test_many_modules_performance(self):
        """Test performance with many registered modules."""
        host = ModuleHost()

        # Register 100 modules. Dummy modules claim no Commands (no @handles
        # methods); only the last one handles StressCommand. Type-routed
        # dispatch is O(1) so registered-but-claim-nothing modules are free.
        for i in range(99):

            @module(name=f"DummyModule{i}")
            class DummyModule(Module):
                pass

            host.register(DummyModule())

        host.register(StressModule())

        # Dispatch commands
        start = time.time()
        num_commands = 100
        for i in range(num_commands):
            command = StressCommand(request=StressInput(value=i))
            response = host.dispatch(command)
            assert response is not None

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
            # Start commands
            tasks = [
                host.dispatch_async(StressCommand(request=StressInput(value=i))) for i in range(5)
            ]
            # Wait for all
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(run_and_shutdown())
        assert all(r is not None for r in results)

        # Shutdown should complete without error
        host.shutdown(wait=True)

    def test_host_has_no_commands_in_progress_surface(self):
        """``commands_in_progress`` was a debug surface and is gone in 1.0.

        If users need in-flight tracking they wire a dedicated middleware.
        """
        host = ModuleHost()
        assert not hasattr(host, "commands_in_progress")
        assert not hasattr(host, "_commands_in_progress")
