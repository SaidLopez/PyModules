"""
PyModules - Event-Driven Modular Architecture for Python

A framework for building scalable, plugin-based applications using
events and modules, inspired by NetModules.

Subpackages:
    pymodules.messaging - Distributed message broker integration
    pymodules.discovery - Service discovery for microservices
    pymodules.fastapi - FastAPI integration
"""

from .config import Metrics, ModuleHostConfig
from .exceptions import (
    ConfigurationError,
    EventHandlingError,
    ModuleRegistrationError,
    PyModulesError,
)
from .health import (
    HealthCheck,
    HealthCheckResult,
    HealthReport,
    HealthStatus,
    create_callable_check,
    create_http_check,
    create_tcp_check,
)
from .host import ModuleHost
from .interfaces import Event, EventInput, EventOutput
from .logging import configure_logging, get_logger
from .module import Module, ModuleMetadata, module
from .resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    DeadLetterEntry,
    DeadLetterQueue,
    Fallback,
    RateLimiter,
    RateLimitExceeded,
    RetryPolicy,
)
from .tracing import (
    OpenTelemetryExporter,
    Span,
    TraceContext,
    Tracer,
    extract_trace_context,
    generate_id,
    get_correlation_id,
    get_current_trace,
    get_tracer,
    inject_trace_context,
    set_tracer,
)

__all__ = [
    # Core interfaces
    "Event",
    "EventInput",
    "EventOutput",
    # Module system
    "Module",
    "module",
    "ModuleMetadata",
    "ModuleHost",
    # Configuration
    "ModuleHostConfig",
    "Metrics",
    # Exceptions
    "PyModulesError",
    "EventHandlingError",
    "ModuleRegistrationError",
    "ConfigurationError",
    "RateLimitExceeded",
    "CircuitBreakerOpen",
    # Logging
    "configure_logging",
    "get_logger",
    # Resilience
    "RateLimiter",
    "CircuitBreaker",
    "CircuitState",
    "RetryPolicy",
    "DeadLetterQueue",
    "DeadLetterEntry",
    "Fallback",
    # Tracing
    "Tracer",
    "TraceContext",
    "Span",
    "get_tracer",
    "set_tracer",
    "get_current_trace",
    "get_correlation_id",
    "inject_trace_context",
    "extract_trace_context",
    "generate_id",
    "OpenTelemetryExporter",
    # Health
    "HealthCheck",
    "HealthCheckResult",
    "HealthReport",
    "HealthStatus",
    "create_http_check",
    "create_tcp_check",
    "create_callable_check",
    # Subpackages (access via pymodules.messaging, pymodules.discovery)
    "messaging",
    "discovery",
]

__version__ = "0.3.0"


# Lazy loading for optional subpackages
def __getattr__(name: str) -> object:
    """Lazy import optional subpackages."""
    if name == "messaging":
        from . import messaging
        return messaging
    if name == "discovery":
        from . import discovery
        return discovery
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
