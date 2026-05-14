"""
Tests for ``IdempotencyMiddleware`` and ``InMemoryIdempotencyStore``.
"""

import asyncio
import time
from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandHandlingError,
    CommandRequest,
    CommandResponse,
    IdempotencyMiddleware,
    InMemoryIdempotencyStore,
    Module,
    ModuleHost,
    ModuleHostConfig,
    handles,
    module,
)


@dataclass
class IdemInput(CommandRequest):
    value: str = ""


@dataclass
class IdemOutput(CommandResponse):
    result: str = ""
    call_index: int = 0


class IdemCommand(Command[IdemInput, IdemOutput]):
    name = "test.idem"


@module(name="IdemModule")
class IdemModule(Module):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @handles(IdemCommand)
    def handle(self, command: IdemCommand) -> IdemOutput:
        self.calls += 1
        return IdemOutput(result=f"r:{command.request.value}", call_index=self.calls)


@module(name="AsyncIdemModule")
class AsyncIdemModule(Module):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self._gate = asyncio.Event()

    @handles(IdemCommand)
    async def handle(self, command: IdemCommand) -> IdemOutput:
        self.calls += 1
        await self._gate.wait()
        return IdemOutput(result=f"r:{command.request.value}", call_index=self.calls)

    def open_gate(self) -> None:
        self._gate.set()


class TestInMemoryIdempotencyStore:
    """Direct tests for the bundled in-memory store."""

    @pytest.mark.asyncio
    async def test_miss_then_hit(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60)
        hit, _ = await store.get("k")
        assert hit is False

        await store.put("k", "v")
        hit, value = await store.get("k")
        assert hit is True
        assert value == "v"

    @pytest.mark.asyncio
    async def test_ttl_expiry_evicts_lazily(self):
        store = InMemoryIdempotencyStore(ttl_seconds=0.05)
        await store.put("k", "v")
        time.sleep(0.06)
        hit, _ = await store.get("k")
        assert hit is False
        assert store.size == 0

    @pytest.mark.asyncio
    async def test_distinct_keys_isolated(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60)
        await store.put("a", 1)
        await store.put("b", 2)
        assert await store.get("a") == (True, 1)
        assert await store.get("b") == (True, 2)

    def test_invalid_ttl_rejected(self):
        with pytest.raises(ValueError):
            InMemoryIdempotencyStore(ttl_seconds=0)
        with pytest.raises(ValueError):
            InMemoryIdempotencyStore(ttl_seconds=-1)


class TestIdempotencyMiddleware:
    """End-to-end behaviour through ``ModuleHost``."""

    def test_command_id_none_passes_through(self):
        mw = IdempotencyMiddleware()
        mod = IdemModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        host.dispatch(IdemCommand(request=IdemInput(value="a")))
        host.dispatch(IdemCommand(request=IdemInput(value="a")))

        assert mod.calls == 2
        assert mw.skipped == 2
        assert mw.hits == 0
        assert mw.misses == 0

    def test_same_id_hits_cache_on_second_dispatch(self):
        mw = IdempotencyMiddleware()
        mod = IdemModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        first = host.dispatch(IdemCommand(request=IdemInput(value="a"), command_id="id-1"))
        second = host.dispatch(IdemCommand(request=IdemInput(value="ignored"), command_id="id-1"))

        assert mod.calls == 1
        assert mw.hits == 1
        assert mw.misses == 1
        # Cached response is replayed verbatim — second request payload is irrelevant.
        assert second.result == first.result
        assert second.call_index == first.call_index == 1

    def test_distinct_ids_run_handler_independently(self):
        mw = IdempotencyMiddleware()
        mod = IdemModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        host.dispatch(IdemCommand(request=IdemInput(value="a"), command_id="id-1"))
        host.dispatch(IdemCommand(request=IdemInput(value="b"), command_id="id-2"))

        assert mod.calls == 2
        assert mw.hits == 0
        assert mw.misses == 2

    def test_exception_not_cached(self):
        mw = IdempotencyMiddleware()

        class FailingModule(Module):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            @handles(IdemCommand)
            def handle(self, command: IdemCommand) -> IdemOutput:
                self.calls += 1
                raise RuntimeError(f"call {self.calls}")

        mod = FailingModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        with pytest.raises(CommandHandlingError):
            host.dispatch(IdemCommand(request=IdemInput(), command_id="id-1"))
        with pytest.raises(CommandHandlingError):
            host.dispatch(IdemCommand(request=IdemInput(), command_id="id-1"))

        # Handler ran both times — failures are not cached.
        assert mod.calls == 2
        assert mw.misses == 2
        assert mw.hits == 0

    def test_ttl_expiry_re_runs_handler(self):
        store = InMemoryIdempotencyStore(ttl_seconds=0.05)
        mw = IdempotencyMiddleware(store=store)
        mod = IdemModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        host.dispatch(IdemCommand(request=IdemInput(value="a"), command_id="id-1"))
        time.sleep(0.06)
        host.dispatch(IdemCommand(request=IdemInput(value="a"), command_id="id-1"))

        assert mod.calls == 2
        assert mw.misses == 2
        assert mw.hits == 0

    @pytest.mark.asyncio
    async def test_concurrent_same_id_runs_handler_once(self):
        """Two concurrent dispatches with the same id: handler runs exactly once."""
        mw = IdempotencyMiddleware()
        mod = AsyncIdemModule()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(mod)

        cmd1 = IdemCommand(request=IdemInput(value="a"), command_id="id-1")
        cmd2 = IdemCommand(request=IdemInput(value="b"), command_id="id-1")

        task1 = asyncio.create_task(host.dispatch_async(cmd1))
        task2 = asyncio.create_task(host.dispatch_async(cmd2))

        # Let both reach the gate, then release.
        await asyncio.sleep(0.01)
        mod.open_gate()

        r1, r2 = await asyncio.gather(task1, task2)

        assert mod.calls == 1
        assert r1.result == r2.result
        assert mw.hits == 1
        assert mw.misses == 1
