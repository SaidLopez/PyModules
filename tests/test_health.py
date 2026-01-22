"""
Tests for health check functionality.
"""

from dataclasses import dataclass

from pymodules import (
    Event,
    EventInput,
    EventOutput,
    HealthCheck,
    HealthCheckResult,
    HealthReport,
    HealthStatus,
    Module,
    ModuleHost,
    ModuleHostConfig,
    create_callable_check,
    module,
)


@dataclass
class HealthInput(EventInput):
    value: str = ""


@dataclass
class HealthOutput(EventOutput):
    result: str = ""


class HealthEvent(Event[HealthInput, HealthOutput]):
    name = "test.health"


@module(name="HealthModule")
class HealthModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, HealthEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, HealthEvent):
            event.output = HealthOutput(result="healthy")
            event.handled = True


class TestHealthCheckResult:
    """Tests for HealthCheckResult."""

    def test_result_creation(self):
        """Health check result can be created."""
        result = HealthCheckResult(
            name="test",
            status=HealthStatus.HEALTHY,
            message="All good",
            duration_ms=10.5,
        )

        assert result.name == "test"
        assert result.status == HealthStatus.HEALTHY
        assert result.message == "All good"
        assert result.duration_ms == 10.5

    def test_to_dict(self):
        """Health check result can be serialized."""
        result = HealthCheckResult(
            name="test",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={"key": "value"},
        )

        data = result.to_dict()

        assert data["name"] == "test"
        assert data["status"] == "healthy"
        assert data["message"] == "OK"
        assert data["details"]["key"] == "value"


class TestHealthReport:
    """Tests for HealthReport."""

    def test_report_creation(self):
        """Health report can be created."""
        report = HealthReport(
            status=HealthStatus.HEALTHY,
            version="1.0.0",
        )

        assert report.status == HealthStatus.HEALTHY
        assert report.version == "1.0.0"
        assert report.uptime_seconds >= 0

    def test_to_dict(self):
        """Health report can be serialized."""
        check = HealthCheckResult(name="db", status=HealthStatus.HEALTHY)
        report = HealthReport(
            status=HealthStatus.HEALTHY,
            checks=[check],
            version="1.0.0",
        )

        data = report.to_dict()

        assert data["status"] == "healthy"
        assert len(data["checks"]) == 1
        assert data["version"] == "1.0.0"


class TestHealthCheck:
    """Tests for HealthCheck manager."""

    def test_health_check_without_host(self):
        """Health check works without host."""
        health = HealthCheck(version="1.0.0")

        report = health.check()

        assert report.status == HealthStatus.HEALTHY
        assert report.version == "1.0.0"

    def test_health_check_with_host(self):
        """Health check monitors host."""
        host = ModuleHost()
        host.register(HealthModule())

        health = HealthCheck(host=host, version="1.0.0")
        report = health.check()

        assert report.status == HealthStatus.HEALTHY
        assert len(report.checks) == 1
        assert report.checks[0].name == "host"

    def test_health_check_degraded_no_modules(self):
        """Health check shows degraded when no modules."""
        host = ModuleHost()

        health = HealthCheck(host=host)
        report = health.check()

        assert report.status == HealthStatus.DEGRADED

    def test_add_custom_check(self):
        """Custom checks can be added."""
        health = HealthCheck()

        def my_check():
            return HealthCheckResult(
                name="custom",
                status=HealthStatus.HEALTHY,
                message="Custom check passed",
            )

        health.add_check("custom", my_check)
        report = health.check()

        assert any(c.name == "custom" for c in report.checks)

    def test_remove_check(self):
        """Checks can be removed."""
        health = HealthCheck()

        def my_check():
            return HealthCheckResult(name="custom", status=HealthStatus.HEALTHY)

        health.add_check("custom", my_check)
        health.remove_check("custom")

        report = health.check()
        assert not any(c.name == "custom" for c in report.checks)

    def test_liveness_probe(self):
        """Liveness probe works."""
        host = ModuleHost()
        health = HealthCheck(host=host)

        report = health.liveness()

        assert report.status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED]

    def test_readiness_probe(self):
        """Readiness probe works."""
        host = ModuleHost()
        host.register(HealthModule())

        health = HealthCheck(host=host)
        report = health.readiness()

        assert report.status == HealthStatus.HEALTHY

    def test_unhealthy_check(self):
        """Unhealthy check affects overall status."""
        health = HealthCheck()

        def failing_check():
            return HealthCheckResult(
                name="failing",
                status=HealthStatus.UNHEALTHY,
                message="Service down",
            )

        health.add_check("failing", failing_check)
        report = health.check()

        assert report.status == HealthStatus.UNHEALTHY

    def test_check_exception_handling(self):
        """Check exceptions are handled gracefully."""
        health = HealthCheck()

        def exploding_check():
            raise RuntimeError("Boom!")

        health.add_check("exploding", exploding_check)
        report = health.check()

        assert report.status == HealthStatus.UNHEALTHY
        assert any("exploding" in c.name for c in report.checks)

    def test_uptime(self):
        """Health check tracks uptime."""
        health = HealthCheck()

        report = health.check()

        assert report.uptime_seconds >= 0


class TestCreateCallableCheck:
    """Tests for create_callable_check helper."""

    def test_healthy_callable(self):
        """Callable returning True is healthy."""
        check = create_callable_check(
            name="test",
            func=lambda: True,
            healthy_message="OK",
            unhealthy_message="Not OK",
        )

        result = check()

        assert result.status == HealthStatus.HEALTHY
        assert result.message == "OK"

    def test_unhealthy_callable(self):
        """Callable returning False is unhealthy."""
        check = create_callable_check(
            name="test",
            func=lambda: False,
            unhealthy_message="Service down",
        )

        result = check()

        assert result.status == HealthStatus.UNHEALTHY
        assert result.message == "Service down"

    def test_exception_callable(self):
        """Callable raising exception is unhealthy."""

        def failing():
            raise ValueError("Error")

        check = create_callable_check(name="test", func=failing)
        result = check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "Error" in result.message


class TestHealthCheckWithMetrics:
    """Tests for health check with metrics."""

    def test_metrics_in_health_details(self):
        """Metrics are included in health check details."""
        config = ModuleHostConfig(enable_metrics=True)
        host = ModuleHost(config=config)
        host.register(HealthModule())

        health = HealthCheck(host=host)
        report = health.check()

        host_check = next(c for c in report.checks if c.name == "host")
        assert "metrics" in host_check.details
