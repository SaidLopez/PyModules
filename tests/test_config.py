"""
Tests for configuration management.
"""

import logging
from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    LifecycleMiddleware,
    MetricsMiddleware,
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
    def handle_config(self, command: ConfigCommand) -> ConfigOutput:
        return ConfigOutput(result=f"processed: {command.request.value}")


class TestModuleHostConfig:
    """Tests for the slimmed-down ``ModuleHostConfig`` dataclass."""

    def test_default_config(self):
        config = ModuleHostConfig()
        assert config.max_workers == 4
        assert config.propagate_exceptions is True
        assert config.log_level == logging.INFO
        assert config.middleware == []

    def test_custom_config(self):
        mw = MetricsMiddleware()
        config = ModuleHostConfig(
            max_workers=16,
            propagate_exceptions=False,
            log_level=logging.DEBUG,
            middleware=[mw],
        )
        assert config.max_workers == 16
        assert config.propagate_exceptions is False
        assert config.log_level == logging.DEBUG
        assert config.middleware == [mw]

    @pytest.mark.parametrize(
        "field",
        [
            "rate_limiter",
            "circuit_breaker",
            "retry_policy",
            "dead_letter_queue",
            "tracer",
            "enable_metrics",
            "enable_tracing",
            "on_error",
            "on_event_start",
            "on_event_end",
        ],
    )
    def test_deleted_fields_absent(self, field):
        """The 1.0 migration removed every transitional flag."""
        assert not hasattr(ModuleHostConfig(), field)

    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("PYMODULES_MAX_WORKERS", raising=False)
        monkeypatch.delenv("PYMODULES_PROPAGATE_EXCEPTIONS", raising=False)
        monkeypatch.delenv("PYMODULES_LOG_LEVEL", raising=False)

        config = ModuleHostConfig.from_env()
        assert config.max_workers == 4
        assert config.propagate_exceptions is True
        assert config.log_level == logging.INFO

    def test_from_env_custom(self, monkeypatch):
        monkeypatch.setenv("PYMODULES_MAX_WORKERS", "8")
        monkeypatch.setenv("PYMODULES_PROPAGATE_EXCEPTIONS", "false")
        monkeypatch.setenv("PYMODULES_LOG_LEVEL", "DEBUG")

        config = ModuleHostConfig.from_env()
        assert config.max_workers == 8
        assert config.propagate_exceptions is False
        assert config.log_level == logging.DEBUG


class TestMetricsMiddleware:
    """Tests for ``MetricsMiddleware`` (counters live on the middleware now)."""

    def test_counters_initially_zero(self):
        mw = MetricsMiddleware()
        assert mw.dispatched == 0
        assert mw.succeeded == 0
        assert mw.failed == 0
        assert mw.unmatched == 0

    def test_tracking(self):
        mw = MetricsMiddleware()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        host.register(ConfigModule())

        host.dispatch(ConfigCommand(request=ConfigInput(value="test")))

        assert mw.dispatched == 1
        assert mw.succeeded == 1
        assert mw.unmatched == 0
        assert mw.failed == 0

    def test_unmatched_command(self):
        mw = MetricsMiddleware()
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        # No module registered.

        host.dispatch(ConfigCommand(request=ConfigInput(value="test")))

        assert mw.dispatched == 1
        assert mw.succeeded == 0
        assert mw.unmatched == 1


class TestLifecycleMiddleware:
    """Tests for the unified ``LifecycleMiddleware``."""

    def test_on_start_callback(self):
        started = []
        lifecycle = LifecycleMiddleware(on_start=started.append)
        host = ModuleHost(config=ModuleHostConfig(middleware=[lifecycle]))
        host.register(ConfigModule())

        command = ConfigCommand(request=ConfigInput(value="test"))
        host.dispatch(command)

        assert started == [command]

    def test_on_end_callback(self):
        ended = []
        lifecycle = LifecycleMiddleware(on_end=lambda c, h: ended.append((c, h)))
        host = ModuleHost(config=ModuleHostConfig(middleware=[lifecycle]))
        host.register(ConfigModule())

        command = ConfigCommand(request=ConfigInput(value="test"))
        host.dispatch(command)

        assert len(ended) == 1
        assert ended[0][0] is command
        assert ended[0][1] is True

    def test_callback_error_does_not_break_dispatch(self):
        def failing(_command):
            raise RuntimeError("Callback failed")

        lifecycle = LifecycleMiddleware(on_start=failing)
        host = ModuleHost(config=ModuleHostConfig(middleware=[lifecycle]))
        host.register(ConfigModule())

        # Should not raise.
        result = host.dispatch(ConfigCommand(request=ConfigInput(value="test")))
        assert isinstance(result, ConfigOutput)
