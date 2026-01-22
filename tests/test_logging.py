"""
Tests for logging functionality.
"""

import logging
from dataclasses import dataclass
from io import StringIO

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, ModuleHostConfig, module
from pymodules.logging import configure_logging, get_logger


@dataclass
class LogInput(EventInput):
    value: str = ""


@dataclass
class LogOutput(EventOutput):
    result: str = ""


class LogEvent(Event[LogInput, LogOutput]):
    name = "test.log"


@module(name="LogTestModule")
class LogTestModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, LogEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, LogEvent):
            event.output = LogOutput(result=f"logged: {event.input.value}")
            event.handled = True


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_default_level(self):
        """Test default logging configuration."""
        log = configure_logging()
        assert log.level == logging.INFO

    def test_configure_debug_level(self):
        """Test debug level configuration."""
        log = configure_logging(level=logging.DEBUG)
        assert log.level == logging.DEBUG

    def test_configure_custom_handler(self):
        """Test custom handler configuration."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)

        log = configure_logging(level=logging.INFO, handler=handler)

        log.info("Test message")
        output = stream.getvalue()

        assert "Test message" in output

    def test_configure_without_timestamps(self):
        """Test configuration without timestamps."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)

        log = configure_logging(level=logging.INFO, handler=handler, format_timestamps=False)

        log.info("Test message")
        output = stream.getvalue()

        # Should not have timestamp format
        assert "INFO" in output
        assert "Test message" in output


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_child_logger(self):
        """Test getting a child logger."""
        child = get_logger("test_component")
        assert child.name == "pymodules.test_component"

    def test_child_logger_inherits_level(self):
        """Test that child logger inherits parent level."""
        configure_logging(level=logging.DEBUG)
        child = get_logger("inherit_test")

        # Child should respect parent's level
        assert child.getEffectiveLevel() == logging.DEBUG


class TestHostLogging:
    """Tests for ModuleHost logging."""

    def test_host_logs_registration(self, caplog):
        """Test that module registration is logged."""
        with caplog.at_level(logging.INFO, logger="pymodules"):
            host = ModuleHost()
            host.register(LogTestModule())

        assert any("Registered module" in record.message for record in caplog.records)
        assert any("LogTestModule" in record.message for record in caplog.records)

    def test_host_logs_unregistration(self, caplog):
        """Test that module unregistration is logged."""
        host = ModuleHost()
        mod = LogTestModule()
        host.register(mod)

        with caplog.at_level(logging.INFO, logger="pymodules"):
            host.unregister(mod)

        assert any("Unregistered module" in record.message for record in caplog.records)

    def test_host_logs_dispatch_at_debug(self, caplog):
        """Test that event dispatch is logged at debug level."""
        config = ModuleHostConfig(log_level=logging.DEBUG)

        with caplog.at_level(logging.DEBUG, logger="pymodules"):
            host = ModuleHost(config=config)
            host.register(LogTestModule())

            event = LogEvent(input=LogInput(value="test"))
            host.handle(event)

        assert any("Dispatching event" in record.message for record in caplog.records)

    def test_host_logs_shutdown(self, caplog):
        """Test that shutdown is logged."""
        with caplog.at_level(logging.INFO, logger="pymodules"):
            host = ModuleHost()
            host.shutdown()

        assert any("Shutting down" in record.message for record in caplog.records)
