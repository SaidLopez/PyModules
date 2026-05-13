"""
PyModules - Event-Driven Modular Architecture for Python

A framework for building scalable, plugin-based applications using
events and modules, inspired by NetModules.

Subpackages:
    pymodules.contrib.messaging - Distributed message broker integration
    pymodules.contrib.discovery - Service discovery for microservices
    pymodules.contrib.api - REST API generation layer
    pymodules.contrib.db - Database abstraction layer
    pymodules.contrib.health - Kubernetes-shaped health checks
    pymodules.fastapi - Legacy FastAPI integration (deprecated; use pymodules.contrib.api)
"""

from .config import Metrics, ModuleHostConfig
from .exceptions import (
    ConfigurationError,
    ConnectionError,
    DatabaseError,
    EventHandlingError,
    ModuleRegistrationError,
    PyModulesError,
    RepositoryError,
)
from .host import ModuleHost
from .interfaces import Event, EventInput, EventOutput
from .logging import configure_logging, get_logger
from .module import Module, ModuleMetadata, module
from .protocols import AsyncEventHandler, EventHandler, EventLike
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
    # Protocols (structural typing)
    "EventLike",
    "EventHandler",
    "AsyncEventHandler",
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
    "DatabaseError",
    "ConnectionError",
    "RepositoryError",
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
]

__version__ = "0.3.0"
