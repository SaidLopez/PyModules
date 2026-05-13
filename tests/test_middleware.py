"""
Tests for the middleware chain itself.

These tests verify the observable contract of the dispatch pipeline:

  - middleware run in the order they were configured (outermost first);
  - a custom middleware can be spliced between defaults at any position;
  - sync ``dispatch()`` on an async handler raises rather than bridging;
  - sync ``dispatch()`` inside a running loop raises rather than nesting.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Middleware,
    Module,
    ModuleHost,
    ModuleHostConfig,
    SyncDispatchInAsyncContextError,
    SyncDispatchOnAsyncHandlerError,
    handles,
    module,
)


@dataclass
class MWInput(CommandRequest):
    value: str = ""


@dataclass
class MWOutput(CommandResponse):
    value: str = ""


class MWCommand(Command[MWInput, MWOutput]):
    name = "test.middleware"


@module(name="MWModule")
class MWModule(Module):
    @handles(MWCommand)
    def handle(self, command: MWCommand) -> MWOutput:
        return MWOutput(value=command.request.value)


def _identity_middleware(name: str, log: list[str]) -> Middleware:
    """Build a middleware that appends ``name`` to ``log`` on each call."""

    async def mw(command, next_call):
        log.append(name)
        return await next_call(command)

    return mw


class TestMiddlewareOrdering:
    """The order configured in ``ModuleHostConfig.middleware`` is observable."""

    def test_runs_in_configured_order(self):
        log: list[str] = []
        chain: list[Middleware] = [
            _identity_middleware("outer", log),
            _identity_middleware("middle", log),
            _identity_middleware("inner", log),
        ]
        host = ModuleHost(config=ModuleHostConfig(middleware=chain))
        host.register(MWModule())

        host.dispatch(MWCommand(request=MWInput(value="hello")))

        assert log == ["outer", "middle", "inner"]

    def test_custom_middleware_between_defaults(self):
        """A custom middleware inserted into ``default_middleware`` runs at its position."""
        from pymodules.resilience import default_middleware

        log: list[str] = []
        chain = default_middleware(
            rate_limit=1000,
            circuit_breaker_threshold=10,
            retry_max=1,
            dlq_size=10,
        )
        # Splice a sentinel middleware at index 2 — between circuit breaker
        # (index 1) and retry (index 2).
        chain.insert(2, _identity_middleware("custom", log))

        host = ModuleHost(config=ModuleHostConfig(middleware=chain))
        host.register(MWModule())

        host.dispatch(MWCommand(request=MWInput(value="hi")))
        assert log == ["custom"]


class TestSyncDispatchSafety:
    """Sync dispatch refuses to bridge async — it raises instead."""

    def test_sync_dispatch_on_async_handler_raises(self):
        @module(name="AsyncModForSyncTest")
        class AsyncMod(Module):
            @handles(MWCommand)
            async def h(self, command: MWCommand) -> MWOutput:
                return MWOutput(value="async")

        host = ModuleHost()
        host.register(AsyncMod())

        with pytest.raises(SyncDispatchOnAsyncHandlerError):
            host.dispatch(MWCommand(request=MWInput()))

    @pytest.mark.asyncio
    async def test_sync_dispatch_in_async_context_raises(self):
        """Calling sync ``dispatch()`` from inside an async function raises."""
        host = ModuleHost()
        host.register(MWModule())

        with pytest.raises(SyncDispatchInAsyncContextError):
            host.dispatch(MWCommand(request=MWInput(value="x")))


class TestMiddlewareCanShortCircuit:
    """A middleware that does not call ``next_call`` skips the handler."""

    def test_short_circuit(self):
        async def short(command, next_call):
            return MWOutput(value="from-middleware")

        host = ModuleHost(config=ModuleHostConfig(middleware=[short]))
        host.register(MWModule())

        response = host.dispatch(MWCommand(request=MWInput(value="ignored")))
        assert response.value == "from-middleware"


@pytest.mark.asyncio
async def test_dispatch_async_returns_handler_response():
    """The composed chain propagates the handler's return value."""
    host = ModuleHost()
    host.register(MWModule())

    response = await host.dispatch_async(MWCommand(request=MWInput(value="echo")))
    assert isinstance(response, MWOutput)
    assert response.value == "echo"
