"""
Tests for error handling and exception propagation.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandHandlingError,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    ModuleHostConfig,
    ModuleRegistrationError,
    handles,
    module,
)


@dataclass
class ErrorInput(CommandRequest):
    should_fail: bool = False


@dataclass
class ErrorOutput(CommandResponse):
    result: str = ""


class ErrorCommand(Command[ErrorInput, ErrorOutput]):
    name = "test.error"


@module(name="ErrorModule", description="A module that can raise errors")
class ErrorModule(Module):
    @handles(ErrorCommand)
    def handle_error(self, command: ErrorCommand) -> ErrorOutput:
        if command.request.should_fail:
            raise ValueError("Intentional test error")
        return ErrorOutput(result="success")


@module(name="FailOnLoad", description="Fails during registration")
class FailOnLoadModule(Module):
    def on_load(self) -> None:
        raise RuntimeError("Failed to load")


class TestErrorPropagation:
    """Tests for exception propagation."""

    def test_propagate_exceptions_true(self):
        """When propagate_exceptions=True, errors should be raised."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        command = ErrorCommand(request=ErrorInput(should_fail=True))

        with pytest.raises(CommandHandlingError) as exc_info:
            host.dispatch(command)

        assert "Intentional test error" in str(exc_info.value)
        assert exc_info.value.command is command
        assert exc_info.value.original_error is not None

    def test_propagate_exceptions_false(self):
        """When propagate_exceptions=False, errors should be suppressed."""
        config = ModuleHostConfig(propagate_exceptions=False)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        command = ErrorCommand(request=ErrorInput(should_fail=True))
        result = host.dispatch(command)

        # Should not raise; with the handler raising before returning, the
        # dispatch result is None.
        assert result is None

    def test_on_error_callback(self):
        """``LifecycleMiddleware.on_error`` fires on handler exceptions."""
        from pymodules import LifecycleMiddleware

        errors = []
        lifecycle = LifecycleMiddleware(on_error=lambda e, c: errors.append((e, c)))

        config = ModuleHostConfig(propagate_exceptions=False, middleware=[lifecycle])
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        command = ErrorCommand(request=ErrorInput(should_fail=True))
        host.dispatch(command)

        assert len(errors) == 1
        assert isinstance(errors[0][0], ValueError)
        assert errors[0][1] is command


class TestRegistrationErrors:
    """Tests for module registration errors."""

    def test_on_load_failure_raises_error(self):
        """Modules that fail on_load should raise ModuleRegistrationError."""
        host = ModuleHost()

        with pytest.raises(ModuleRegistrationError):
            host.register(FailOnLoadModule())

        # Module should not be registered
        assert len(host.modules) == 0

    def test_registration_rollback(self):
        """Failed registration should not leave partial state."""
        host = ModuleHost()

        try:
            host.register(FailOnLoadModule())
        except ModuleRegistrationError:
            pass

        # Host property should be cleared
        assert len(host.modules) == 0


class TestCommandHandlingErrorDetails:
    """Tests for CommandHandlingError attributes."""

    def test_error_includes_command(self):
        """CommandHandlingError should include the command."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        command = ErrorCommand(request=ErrorInput(should_fail=True))

        try:
            host.dispatch(command)
        except CommandHandlingError as e:
            assert e.command is command
            assert e.module is not None
            assert e.module.metadata.name == "ErrorModule"

    def test_error_string_representation(self):
        """Test string representation of CommandHandlingError."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        command = ErrorCommand(request=ErrorInput(should_fail=True))

        try:
            host.dispatch(command)
        except CommandHandlingError as e:
            error_str = str(e)
            assert "ErrorModule" in error_str
            assert "test.error" in error_str
            assert "ValueError" in error_str
