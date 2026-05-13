"""
PyModules - In-process command dispatch for Python

A framework for building scalable, plugin-based applications using
commands and modules, inspired by NetModules.

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
    CommandHandlingError,
    ConfigurationError,
    ConnectionError,
    DatabaseError,
    DuplicateCommandError,
    ModuleRegistrationError,
    PyModulesError,
    RepositoryError,
)
from .host import ModuleHost
from .interfaces import Command, CommandRequest, CommandResponse
from .logging import configure_logging, get_logger
from .module import Module, ModuleMetadata, handles, module
from .protocols import AsyncCommandHandler, CommandHandler, CommandLike
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
    "Command",
    "CommandRequest",
    "CommandResponse",
    # Protocols (structural typing)
    "CommandLike",
    "CommandHandler",
    "AsyncCommandHandler",
    # Module system
    "Module",
    "module",
    "ModuleMetadata",
    "ModuleHost",
    "handles",
    # Configuration
    "ModuleHostConfig",
    "Metrics",
    # Exceptions
    "PyModulesError",
    "CommandHandlingError",
    "ModuleRegistrationError",
    "DuplicateCommandError",
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
