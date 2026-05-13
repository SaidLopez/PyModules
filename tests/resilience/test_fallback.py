"""
Tests for ``Fallback`` (decorator) and ``FallbackMiddleware``.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Fallback,
    FallbackMiddleware,
    Module,
    ModuleHost,
    ModuleHostConfig,
    handles,
    module,
)


@dataclass
class FBInput(CommandRequest):
    should_fail: bool = False


@dataclass
class FBOutput(CommandResponse):
    result: str = ""


class FBCommand(Command[FBInput, FBOutput]):
    name = "test.fallback"


@module(name="FBModule")
class FBModule(Module):
    @handles(FBCommand)
    def handle(self, command: FBCommand) -> FBOutput:
        if command.request.should_fail:
            raise ValueError("Failed")
        return FBOutput(result="primary")


class TestFallback:
    def test_returns_default_value(self):
        fallback = Fallback(default_value="fallback_value", log_errors=False)

        @fallback
        def failing():
            raise ValueError("Failed")

        assert failing() == "fallback_value"

    def test_returns_normal_value(self):
        fallback = Fallback(default_value="fallback_value")

        @fallback
        def working():
            return "normal_value"

        assert working() == "normal_value"

    def test_fallback_function(self):
        fallback = Fallback(fallback_func=lambda: {"status": "degraded"}, log_errors=False)

        @fallback
        def failing():
            raise ValueError("Failed")

        assert failing() == {"status": "degraded"}

    def test_specific_exceptions(self):
        fallback = Fallback(default_value="fallback", exceptions=(ValueError,), log_errors=False)

        @fallback
        def value_error():
            raise ValueError("Caught")

        @fallback
        def type_error():
            raise TypeError("Not caught")

        assert value_error() == "fallback"
        with pytest.raises(TypeError):
            type_error()


class TestFallbackMiddleware:
    def test_integration_returns_default(self):
        mw = FallbackMiddleware(
            default_value=FBOutput(result="fallback"),
            log_errors=False,
        )
        config = ModuleHostConfig(
            middleware=[mw],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)
        host.register(FBModule())

        response = host.dispatch(FBCommand(request=FBInput(should_fail=True)))
        assert response.result == "fallback"
        assert mw.fallback_count == 1


class TestFallbackIgnoresFrameworkSignals:
    """Framework signals must propagate, not be masked by a fallback."""

    def test_unknown_command_propagates_through_fallback(self):
        from pymodules import UnknownCommandError

        mw = FallbackMiddleware(default_value=FBOutput(result="fallback"))
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        # No module registered — terminal raises UnknownCommandError.
        with pytest.raises(UnknownCommandError):
            host.dispatch(FBCommand(request=FBInput()))
        assert mw.fallback_count == 0
