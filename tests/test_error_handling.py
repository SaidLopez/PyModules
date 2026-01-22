"""
Tests for error handling and exception propagation.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Event,
    EventHandlingError,
    EventInput,
    EventOutput,
    Module,
    ModuleHost,
    ModuleHostConfig,
    ModuleRegistrationError,
    module,
)


@dataclass
class ErrorInput(EventInput):
    should_fail: bool = False


@dataclass
class ErrorOutput(EventOutput):
    result: str = ""


class ErrorEvent(Event[ErrorInput, ErrorOutput]):
    name = "test.error"


@module(name="ErrorModule", description="A module that can raise errors")
class ErrorModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, ErrorEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, ErrorEvent):
            if event.input.should_fail:
                raise ValueError("Intentional test error")
            event.output = ErrorOutput(result="success")
            event.handled = True


@module(name="FailOnLoad", description="Fails during registration")
class FailOnLoadModule(Module):
    def on_load(self) -> None:
        raise RuntimeError("Failed to load")

    def can_handle(self, event: Event) -> bool:
        return False

    def handle(self, event: Event) -> None:
        pass


class TestErrorPropagation:
    """Tests for exception propagation."""

    def test_propagate_exceptions_true(self):
        """When propagate_exceptions=True, errors should be raised."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        event = ErrorEvent(input=ErrorInput(should_fail=True))

        with pytest.raises(EventHandlingError) as exc_info:
            host.handle(event)

        assert "Intentional test error" in str(exc_info.value)
        assert exc_info.value.event is event
        assert exc_info.value.original_error is not None

    def test_propagate_exceptions_false(self):
        """When propagate_exceptions=False, errors should be suppressed."""
        config = ModuleHostConfig(propagate_exceptions=False)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        event = ErrorEvent(input=ErrorInput(should_fail=True))
        result = host.handle(event)

        # Should not raise, but event should not be marked as handled
        assert result is event
        assert not event.handled

    def test_on_error_callback(self):
        """Test that on_error callback is called on exceptions."""
        errors = []

        def error_handler(error, event):
            errors.append((error, event))

        config = ModuleHostConfig(propagate_exceptions=False, on_error=error_handler)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        event = ErrorEvent(input=ErrorInput(should_fail=True))
        host.handle(event)

        assert len(errors) == 1
        assert isinstance(errors[0][0], ValueError)
        assert errors[0][1] is event


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


class TestEventHandlingErrorDetails:
    """Tests for EventHandlingError attributes."""

    def test_error_includes_event(self):
        """EventHandlingError should include the event."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        event = ErrorEvent(input=ErrorInput(should_fail=True))

        try:
            host.handle(event)
        except EventHandlingError as e:
            assert e.event is event
            assert e.module is not None
            assert e.module.metadata.name == "ErrorModule"

    def test_error_string_representation(self):
        """Test string representation of EventHandlingError."""
        config = ModuleHostConfig(propagate_exceptions=True)
        host = ModuleHost(config=config)
        host.register(ErrorModule())

        event = ErrorEvent(input=ErrorInput(should_fail=True))

        try:
            host.handle(event)
        except EventHandlingError as e:
            error_str = str(e)
            assert "ErrorModule" in error_str
            assert "test.error" in error_str
            assert "ValueError" in error_str
