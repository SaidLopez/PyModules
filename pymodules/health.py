"""
Health check support for PyModules framework.

Provides health check endpoints for Kubernetes, load balancers, and monitoring.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .host import ModuleHost

from .logging import get_logger

health_logger = get_logger("health")


class HealthStatus(Enum):
    """Health check status values."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthCheckResult:
    """Result of a health check."""

    name: str
    status: HealthStatus
    message: str = ""
    duration_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "details": self.details,
        }


@dataclass
class HealthReport:
    """Complete health report for the service."""

    status: HealthStatus
    checks: list[HealthCheckResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    version: str = ""
    uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status.value,
            "timestamp": self.timestamp,
            "version": self.version,
            "uptime_seconds": self.uptime_seconds,
            "checks": [c.to_dict() for c in self.checks],
        }


class HealthCheck:
    """
    Health check manager for PyModules.

    Provides liveness and readiness probes compatible with Kubernetes.

    Example:
        health = HealthCheck(host)

        # Add custom checks
        health.add_check("database", check_database_connection)
        health.add_check("cache", check_redis_connection)

        # Get health report
        report = health.check()
        if report.status == HealthStatus.HEALTHY:
            print("All systems go!")

        # Use with FastAPI
        @app.get("/health")
        def health_endpoint():
            return health.check().to_dict()

        @app.get("/health/live")
        def liveness():
            return health.liveness().to_dict()

        @app.get("/health/ready")
        def readiness():
            return health.readiness().to_dict()
    """

    def __init__(
        self,
        host: "ModuleHost | None" = None,
        version: str = "",
    ):
        """
        Initialize health check manager.

        Args:
            host: Optional ModuleHost to monitor.
            version: Service version string.
        """
        self._host = host
        self._version = version
        self._start_time = time.time()
        self._checks: dict[str, Callable[[], HealthCheckResult]] = {}
        self._liveness_checks: dict[str, Callable[[], HealthCheckResult]] = {}
        self._readiness_checks: dict[str, Callable[[], HealthCheckResult]] = {}

        # Add default host check if host provided
        if host:
            self.add_check("host", self._check_host, liveness=True, readiness=True)

    def _check_host(self) -> HealthCheckResult:
        """Default check for ModuleHost health."""
        start = time.time()

        if not self._host:
            return HealthCheckResult(
                name="host",
                status=HealthStatus.UNHEALTHY,
                message="No host configured",
            )

        try:
            modules = len(self._host.modules)
            events_in_progress = len(self._host.events_in_progress)

            details: dict[str, Any] = {
                "modules_registered": modules,
                "events_in_progress": events_in_progress,
            }

            # Add metrics if available
            if self._host.metrics:
                details["metrics"] = self._host.metrics.to_dict()

            # Check for concerning states
            if modules == 0:
                return HealthCheckResult(
                    name="host",
                    status=HealthStatus.DEGRADED,
                    message="No modules registered",
                    duration_ms=(time.time() - start) * 1000,
                    details=details,
                )

            return HealthCheckResult(
                name="host",
                status=HealthStatus.HEALTHY,
                message=f"{modules} modules registered",
                duration_ms=(time.time() - start) * 1000,
                details=details,
            )

        except Exception as e:
            return HealthCheckResult(
                name="host",
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                duration_ms=(time.time() - start) * 1000,
            )

    def add_check(
        self,
        name: str,
        check_func: Callable[[], HealthCheckResult],
        liveness: bool = False,
        readiness: bool = True,
    ) -> None:
        """
        Add a health check.

        Args:
            name: Name of the check.
            check_func: Function that returns HealthCheckResult.
            liveness: Include in liveness probes.
            readiness: Include in readiness probes.
        """
        self._checks[name] = check_func
        if liveness:
            self._liveness_checks[name] = check_func
        if readiness:
            self._readiness_checks[name] = check_func

    def remove_check(self, name: str) -> None:
        """Remove a health check."""
        self._checks.pop(name, None)
        self._liveness_checks.pop(name, None)
        self._readiness_checks.pop(name, None)

    def _run_checks(self, checks: dict[str, Callable[[], HealthCheckResult]]) -> HealthReport:
        """Run a set of health checks and compile report."""
        results: list[HealthCheckResult] = []
        overall_status = HealthStatus.HEALTHY

        for name, check_func in checks.items():
            try:
                result = check_func()
            except Exception as e:
                result = HealthCheckResult(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Check failed: {e}",
                )
                health_logger.error("Health check %s failed: %s", name, e)

            results.append(result)

            # Update overall status (worst wins)
            if result.status == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            elif (
                result.status == HealthStatus.DEGRADED and overall_status != HealthStatus.UNHEALTHY
            ):
                overall_status = HealthStatus.DEGRADED

        return HealthReport(
            status=overall_status,
            checks=results,
            version=self._version,
            uptime_seconds=time.time() - self._start_time,
        )

    def check(self) -> HealthReport:
        """Run all health checks."""
        return self._run_checks(self._checks)

    def liveness(self) -> HealthReport:
        """
        Run liveness checks.

        Liveness probes determine if the application is running.
        If this fails, the container should be restarted.
        """
        if not self._liveness_checks:
            # Default liveness: just confirm we're running
            return HealthReport(
                status=HealthStatus.HEALTHY,
                version=self._version,
                uptime_seconds=time.time() - self._start_time,
            )
        return self._run_checks(self._liveness_checks)

    def readiness(self) -> HealthReport:
        """
        Run readiness checks.

        Readiness probes determine if the application can serve traffic.
        If this fails, traffic should be routed elsewhere.
        """
        return self._run_checks(self._readiness_checks)


# =============================================================================
# Helper functions for common checks
# =============================================================================


def create_http_check(
    name: str,
    url: str,
    timeout: float = 5.0,
) -> Callable[[], HealthCheckResult]:
    """
    Create an HTTP health check.

    Args:
        name: Name of the check.
        url: URL to check.
        timeout: Request timeout in seconds.

    Returns:
        A check function.
    """

    def check() -> HealthCheckResult:
        import urllib.error
        import urllib.request

        start = time.time()
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.status
                duration = (time.time() - start) * 1000

                if 200 <= status_code < 300:
                    return HealthCheckResult(
                        name=name,
                        status=HealthStatus.HEALTHY,
                        message=f"HTTP {status_code}",
                        duration_ms=duration,
                        details={"url": url, "status_code": status_code},
                    )
                else:
                    return HealthCheckResult(
                        name=name,
                        status=HealthStatus.UNHEALTHY,
                        message=f"HTTP {status_code}",
                        duration_ms=duration,
                        details={"url": url, "status_code": status_code},
                    )
        except urllib.error.URLError as e:
            return HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                duration_ms=(time.time() - start) * 1000,
                details={"url": url},
            )
        except Exception as e:
            return HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                duration_ms=(time.time() - start) * 1000,
            )

    return check


def create_tcp_check(
    name: str,
    host: str,
    port: int,
    timeout: float = 5.0,
) -> Callable[[], HealthCheckResult]:
    """
    Create a TCP connectivity check.

    Args:
        name: Name of the check.
        host: Host to connect to.
        port: Port to connect to.
        timeout: Connection timeout in seconds.

    Returns:
        A check function.
    """

    def check() -> HealthCheckResult:
        import socket

        start = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()

            return HealthCheckResult(
                name=name,
                status=HealthStatus.HEALTHY,
                message=f"Connected to {host}:{port}",
                duration_ms=(time.time() - start) * 1000,
                details={"host": host, "port": port},
            )
        except OSError as e:
            return HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                duration_ms=(time.time() - start) * 1000,
                details={"host": host, "port": port},
            )

    return check


def create_callable_check(
    name: str,
    func: Callable[[], bool],
    healthy_message: str = "Check passed",
    unhealthy_message: str = "Check failed",
) -> Callable[[], HealthCheckResult]:
    """
    Create a health check from a simple callable.

    Args:
        name: Name of the check.
        func: Function that returns True if healthy.
        healthy_message: Message when healthy.
        unhealthy_message: Message when unhealthy.

    Returns:
        A check function.
    """

    def check() -> HealthCheckResult:
        start = time.time()
        try:
            is_healthy = func()
            return HealthCheckResult(
                name=name,
                status=HealthStatus.HEALTHY if is_healthy else HealthStatus.UNHEALTHY,
                message=healthy_message if is_healthy else unhealthy_message,
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                duration_ms=(time.time() - start) * 1000,
            )

    return check
