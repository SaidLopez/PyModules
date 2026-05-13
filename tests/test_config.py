"""
Tests for configuration management.
"""

import logging
from dataclasses import dataclass

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Metrics,
    Module,
    ModuleHost,
    ModuleHostConfig,
    handles,
    module,
)


@dataclass
class ConfigInput(CommandRequest):
    value: str = ""


@dataclass
class ConfigOutput(CommandResponse):
    result: str = ""


class ConfigCommand(Command[ConfigInput, ConfigOutput]):
    name = "test.config"


@module(name="ConfigModule")
class ConfigModule(Module):
    @handles(ConfigCommand)
    def handle_config(self, command: ConfigCommand) -> None:
        command.output = ConfigOutput(result=f"processed: {command.input.value}")
        command.handled = True


class TestModuleHostConfig:
    """Tests for ModuleHostConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = ModuleHostConfig()
        assert config.max_workers == 4
        assert config.propagate_exceptions is True
        assert config.log_level == logging.INFO
        assert config.on_error is None
        assert config.on_event_start is None
        assert config.on_event_end is None
        assert config.enable_metrics is False

    def test_custom_config(self):
        """Test custom configuration values."""
        error_handler = lambda e, c: None

        config = ModuleHostConfig(
            max_workers=16,
            propagate_exceptions=False,
            log_level=logging.DEBUG,
            on_error=error_handler,
            enable_metrics=True,
        )

        assert config.max_workers == 16
        assert config.propagate_exceptions is False
        assert config.log_level == logging.DEBUG
        assert config.on_error is error_handler
        assert config.enable_metrics is True

    def test_from_env_defaults(self, monkeypatch):
        """Test from_env with no environment variables."""
        # Clear any existing env vars
        monkeypatch.delenv("PYMODULES_MAX_WORKERS", raising=False)
        monkeypatch.delenv("PYMODULES_PROPAGATE_EXCEPTIONS", raising=False)
        monkeypatch.delenv("PYMODULES_LOG_LEVEL", raising=False)
        monkeypatch.delenv("PYMODULES_ENABLE_METRICS", raising=False)

        config = ModuleHostConfig.from_env()
        assert config.max_workers == 4
        assert config.propagate_exceptions is True
        assert config.log_level == logging.INFO

    def test_from_env_custom(self, monkeypatch):
        """Test from_env with custom environment variables."""
        monkeypatch.setenv("PYMODULES_MAX_WORKERS", "8")
        monkeypatch.setenv("PYMODULES_PROPAGATE_EXCEPTIONS", "false")
        monkeypatch.setenv("PYMODULES_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("PYMODULES_ENABLE_METRICS", "true")

        config = ModuleHostConfig.from_env()
        assert config.max_workers == 8
        assert config.propagate_exceptions is False
        assert config.log_level == logging.DEBUG
        assert config.enable_metrics is True


class TestMetrics:
    """Tests for metrics collection."""

    def test_metrics_disabled_by_default(self):
        """Metrics should be disabled by default."""
        host = ModuleHost()
        assert host.metrics is None

    def test_metrics_enabled(self):
        """Test metrics when enabled."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        assert host.metrics is not None
        assert host.metrics.events_dispatched == 0
        assert host.metrics.modules_registered == 1

    def test_metrics_tracking(self):
        """Test that metrics are properly tracked."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        # Dispatch handled command
        command = ConfigCommand(input=ConfigInput(value="test"))
        host.dispatch(command)

        assert host.metrics.events_dispatched == 1
        assert host.metrics.events_handled == 1
        assert host.metrics.events_unhandled == 0

    def test_metrics_unhandled_command(self):
        """Test metrics for unhandled commands."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        # No modules registered

        command = ConfigCommand(input=ConfigInput(value="test"))
        host.dispatch(command)

        assert host.metrics.events_dispatched == 1
        assert host.metrics.events_handled == 0
        assert host.metrics.events_unhandled == 1

    def test_metrics_to_dict(self):
        """Test metrics serialization."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        command = ConfigCommand(input=ConfigInput(value="test"))
        host.dispatch(command)

        metrics_dict = host.metrics.to_dict()
        assert isinstance(metrics_dict, dict)
        assert metrics_dict["events_dispatched"] == 1
        assert metrics_dict["events_handled"] == 1
        assert metrics_dict["modules_registered"] == 1

    def test_metrics_reset(self):
        """Test metrics reset functionality."""
        metrics = Metrics(
            events_dispatched=10,
            events_handled=8,
            events_unhandled=2,
            events_failed=0,
            modules_registered=5,
        )

        metrics.reset()

        assert metrics.events_dispatched == 0
        assert metrics.events_handled == 0
        assert metrics.events_unhandled == 0
        assert metrics.events_failed == 0
        # modules_registered should NOT be reset
        assert metrics.modules_registered == 5


class TestLifecycleHooks:
    """Tests for lifecycle callbacks."""

    def test_on_event_start_callback(self):
        """Test on_event_start is called."""
        started = []

        def on_start(command):
            started.append(command)

        config = ModuleHostConfig(on_event_start=on_start)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        command = ConfigCommand(input=ConfigInput(value="test"))
        host.dispatch(command)

        assert len(started) == 1
        assert started[0] is command

    def test_on_event_end_callback(self):
        """Test on_event_end is called."""
        ended = []

        def on_end(command, handled):
            ended.append((command, handled))

        config = ModuleHostConfig(on_event_end=on_end)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        command = ConfigCommand(input=ConfigInput(value="test"))
        host.dispatch(command)

        assert len(ended) == 1
        assert ended[0][0] is command
        assert ended[0][1] is True

    def test_callback_error_does_not_break_dispatch(self):
        """Test that callback errors don't break dispatch."""

        def failing_callback(command):
            raise RuntimeError("Callback failed")

        config = ModuleHostConfig(on_event_start=failing_callback)
        host = ModuleHost(config=config)
        host.register(ConfigModule())

        command = ConfigCommand(input=ConfigInput(value="test"))
        # Should not raise
        result = host.dispatch(command)

        assert result.handled is True
