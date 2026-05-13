"""
FastAPI integration layer for PyModules.

Provides utilities to expose PyModules commands as HTTP endpoints,
with built-in health checks, metrics, and tracing middleware.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from ..contrib.health import HealthCheck, HealthStatus
from ..host import ModuleHost
from ..interfaces import Command, CommandRequest, CommandResponse

# FastAPI is optional - only import if available
try:
    from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
    from pydantic import create_model
    from starlette.middleware.base import BaseHTTPMiddleware

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    FastAPI = None  # type: ignore
    APIRouter = None  # type: ignore
    Request = None  # type: ignore
    Response = None  # type: ignore


def _dataclass_to_pydantic(dc_class: type, name: str | None = None) -> type:
    """Convert a dataclass to a Pydantic model for FastAPI."""
    if not FASTAPI_AVAILABLE:
        raise ImportError("FastAPI is required. Install with: pip install pymodules[fastapi]")

    if not is_dataclass(dc_class):
        raise ValueError(f"{dc_class} is not a dataclass")

    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_type in dc_class.__annotations__.items():
        default = getattr(dc_class, field_name, ...)
        if callable(default) or default is None:
            default = None
        fields[field_name] = (field_type, default)

    model_name = name or dc_class.__name__ + "Model"
    return create_model(model_name, **fields)  # type: ignore


class TracingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that adds correlation IDs to requests and responses.

    Injects X-Correlation-ID header into responses and makes correlation
    ID available throughout the request lifecycle.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get or generate correlation ID
        correlation_id = request.headers.get("X-Correlation-ID")
        if not correlation_id:
            correlation_id = uuid.uuid4().hex[:16]

        # Store in request state for access by handlers
        request.state.correlation_id = correlation_id

        # Process request
        response = await call_next(request)

        # Add correlation ID to response headers
        response.headers["X-Correlation-ID"] = correlation_id

        return response


class PyModulesAPI:
    """
    FastAPI integration for PyModules.

    Wraps a ModuleHost and provides utilities to expose commands
    as HTTP endpoints, with built-in health checks and metrics.

    Example:
        from fastapi import FastAPI
        from pymodules import ModuleHost
        from pymodules.fastapi import PyModulesAPI

        host = ModuleHost()
        host.register(GreeterModule())

        app = FastAPI()
        api = PyModulesAPI(host, version="1.0.0")

        # Add standard endpoints
        api.add_health_endpoints(app)
        api.add_metrics_endpoint(app)
        api.add_tracing_middleware(app)

        # Add command endpoint
        api.add_command_endpoint(app, "/greet", GreetCommand, GreetRequest, GreetResponse)
    """

    def __init__(self, host: ModuleHost, version: str = ""):
        """
        Initialize PyModulesAPI.

        Args:
            host: The ModuleHost to wrap.
            version: Service version for health checks.
        """
        if not FASTAPI_AVAILABLE:
            raise ImportError("FastAPI is required. Install with: pip install pymodules[fastapi]")
        self.host = host
        self.version = version
        self._health_check: HealthCheck | None = None
        self._start_time = time.time()

    @property
    def health_check(self) -> HealthCheck:
        """Get or create the health check instance."""
        if self._health_check is None:
            self._health_check = HealthCheck(host=self.host, version=self.version)
        return self._health_check

    async def dispatch(self, command: Command) -> Any:
        """
        Dispatch a command through the ModuleHost asynchronously.

        Args:
            command: The command to dispatch

        Returns:
            The CommandResponse returned by the claiming Module.

        Raises:
            HTTPException: If no module handled the command
        """
        response = await self.host.dispatch_async(command)

        if response is None:
            raise HTTPException(
                status_code=404, detail=f"No module found to handle command: {command.name}"
            )

        return response

    def add_command_endpoint(
        self,
        app: "FastAPI",
        path: str,
        command_class: type[Command],
        input_class: type[CommandRequest],
        output_class: type[CommandResponse] | None = None,
        method: str = "POST",
        **route_kwargs: Any,
    ) -> None:
        """
        Add an HTTP endpoint that dispatches a command.

        Args:
            app: FastAPI application
            path: URL path for the endpoint
            command_class: The Command class to instantiate
            input_class: The CommandRequest class (used for request body)
            output_class: The CommandResponse class (used for response model)
            method: HTTP method (POST, GET, etc.)
            **route_kwargs: Additional arguments for app.add_api_route
        """
        # Create Pydantic models from dataclasses
        InputModel = _dataclass_to_pydantic(input_class, f"{command_class.__name__}Request")

        if output_class:
            OutputModel = _dataclass_to_pydantic(output_class, f"{command_class.__name__}Response")
        else:
            OutputModel = None

        async def endpoint(request: Request, body: InputModel) -> Any:  # type: ignore
            # Convert Pydantic model back to dataclass
            request_data = input_class(**body.model_dump())
            command = command_class(request=request_data)

            # Add correlation ID from request if available
            if hasattr(request.state, "correlation_id"):
                command.meta["correlation_id"] = request.state.correlation_id

            response = await self.dispatch(command)

            if response is not None and is_dataclass(response):
                return asdict(response)
            return {"handled": response is not None}

        app.add_api_route(
            path, endpoint, methods=[method], response_model=OutputModel, **route_kwargs
        )

    def add_health_endpoints(
        self,
        app: "FastAPI",
        prefix: str = "/health",
        tags: list[str] | None = None,
    ) -> None:
        """
        Add health check endpoints to the FastAPI app.

        Adds:
            - GET {prefix} - Full health check
            - GET {prefix}/live - Liveness probe (is the app running?)
            - GET {prefix}/ready - Readiness probe (can it serve traffic?)

        Args:
            app: FastAPI application
            prefix: URL prefix for health endpoints
            tags: OpenAPI tags for the endpoints
        """
        tags = tags or ["Health"]

        @app.get(prefix, tags=tags, summary="Full health check")
        async def health() -> dict[str, Any]:
            """
            Full health check including all registered checks.

            Returns overall status, individual check results, and metadata.
            """
            report = self.health_check.check()
            response = report.to_dict()

            # Return appropriate status code
            if report.status == HealthStatus.UNHEALTHY:
                return JSONResponse(content=response, status_code=503)
            return response

        @app.get(f"{prefix}/live", tags=tags, summary="Liveness probe")
        async def liveness() -> dict[str, Any]:
            """
            Liveness probe for Kubernetes.

            Checks if the application is running. If this fails,
            the container should be restarted.
            """
            report = self.health_check.liveness()
            response = report.to_dict()

            if report.status == HealthStatus.UNHEALTHY:
                return JSONResponse(content=response, status_code=503)
            return response

        @app.get(f"{prefix}/ready", tags=tags, summary="Readiness probe")
        async def readiness() -> dict[str, Any]:
            """
            Readiness probe for Kubernetes.

            Checks if the application can serve traffic. If this fails,
            traffic should be routed elsewhere.
            """
            report = self.health_check.readiness()
            response = report.to_dict()

            if report.status == HealthStatus.UNHEALTHY:
                return JSONResponse(content=response, status_code=503)
            return response

    def add_metrics_endpoint(
        self,
        app: "FastAPI",
        path: str = "/metrics",
        tags: list[str] | None = None,
    ) -> None:
        """
        Add metrics endpoint to the FastAPI app.

        Args:
            app: FastAPI application
            path: URL path for the metrics endpoint
            tags: OpenAPI tags for the endpoint
        """
        tags = tags or ["Metrics"]

        @app.get(path, tags=tags, summary="Get metrics")
        async def metrics() -> dict[str, Any]:
            """
            Get current metrics from the ModuleHost.

            Returns command processing statistics including dispatched,
            handled, failed, retried, and rate-limited counts.
            """
            result: dict[str, Any] = {
                "uptime_seconds": time.time() - self._start_time,
                "version": self.version,
            }

            if self.host.metrics:
                result["metrics"] = self.host.metrics.to_dict()
            else:
                result["metrics"] = {"metrics_enabled": False}

            # Add module info
            result["modules"] = {
                "count": len(self.host.modules),
                "names": [m.metadata.name for m in self.host.modules],
            }

            # Add resilience status if configured
            config = self.host.config
            result["resilience"] = {
                "rate_limiter": config.rate_limiter is not None,
                "circuit_breaker": config.circuit_breaker is not None,
                "retry_policy": config.retry_policy is not None,
                "dead_letter_queue": config.dead_letter_queue is not None,
            }

            if config.circuit_breaker:
                result["circuit_breaker_state"] = config.circuit_breaker.state.value

            if config.dead_letter_queue is not None:
                result["dlq_size"] = len(config.dead_letter_queue)

            return result

    def add_tracing_middleware(self, app: "FastAPI") -> None:
        """
        Add tracing middleware to the FastAPI app.

        Injects X-Correlation-ID header into all responses.
        If the request includes X-Correlation-ID, it will be preserved.
        Otherwise, a new correlation ID is generated.

        Args:
            app: FastAPI application
        """
        app.add_middleware(TracingMiddleware)

    def add_all_endpoints(
        self,
        app: "FastAPI",
        health_prefix: str = "/health",
        metrics_path: str = "/metrics",
    ) -> None:
        """
        Add all standard endpoints (health, metrics) and tracing middleware.

        Convenience method to set up a production-ready API.

        Args:
            app: FastAPI application
            health_prefix: URL prefix for health endpoints
            metrics_path: URL path for metrics endpoint
        """
        self.add_health_endpoints(app, prefix=health_prefix)
        self.add_metrics_endpoint(app, path=metrics_path)
        self.add_tracing_middleware(app)


def command_endpoint(
    command_class: type[Command],
    input_class: type[CommandRequest],
    path: str | None = None,
) -> Callable[[Callable], Callable]:
    """
    Decorator to mark a function as a command endpoint.

    This is syntactic sugar for manual endpoint creation.

    Example:
        @command_endpoint(GreetCommand, GreetRequest, "/greet")
        async def greet_handler(command: GreetCommand) -> GreetResponse:
            return GreetResponse(...)
    """

    def decorator(func: Callable) -> Callable:
        # Store metadata on the function for later registration
        func._pymodules_command = {  # type: ignore[attr-defined]
            "command_class": command_class,
            "input_class": input_class,
            "path": path or f"/{command_class.name.replace('.', '/')}",
        }
        return func

    return decorator
