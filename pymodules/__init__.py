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
    pymodules.contrib.tracing - Tracing exporters (e.g. OpenTelemetry)
"""

from .agent import (
    Agent,
    AgentError,
    AgentFailed,
    AgentNotRegistered,
    AgentRun,
    AgentRunStuck,
    AgentSpawner,
    AgentSpawnRejected,
    scheduled,
)
from .agent_state import AgentStateStore, InMemoryAgentStateStore
from .config import ModuleHostConfig
from .eventbus import EventBus, EventHandler
from .exceptions import (
    CommandHandlingError,
    ConfigurationError,
    ConnectionError,
    DatabaseError,
    DuplicateCommandError,
    ModuleRegistrationError,
    PyModulesError,
    PyModulesSignal,
    RepositoryError,
    SyncDispatchInAsyncContextError,
    SyncDispatchOnAsyncHandlerError,
    UnknownCommandError,
)
from .host import ModuleHost
from .interfaces import Command, CommandContext, CommandRequest, CommandResponse, Event
from .logging import configure_logging, get_logger
from .middleware import Middleware, NextCall
from .module import Module, ModuleMetadata, handles, module, subscribes
from .resilience import (
    CircuitBreaker,
    CircuitBreakerMiddleware,
    CircuitBreakerOpen,
    CircuitState,
    DeadLetterEntry,
    DeadLetterQueue,
    DLQMiddleware,
    Fallback,
    FallbackMiddleware,
    IdempotencyMiddleware,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RateLimitExceeded,
    RateLimitMiddleware,
    RetryMiddleware,
    RetryPolicy,
    default_middleware,
    default_middleware_from_env,
)
from .tracing import (
    LifecycleMiddleware,
    MetricsMiddleware,
    Span,
    TraceContext,
    Tracer,
    TracingMiddleware,
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
    "CommandContext",
    "CommandRequest",
    "CommandResponse",
    "Event",
    # Module system
    "Module",
    "module",
    "ModuleMetadata",
    "ModuleHost",
    "handles",
    "subscribes",
    # Agent primitive (ADR-0008)
    "Agent",
    "AgentRun",
    "AgentSpawner",
    "AgentStateStore",
    "InMemoryAgentStateStore",
    "scheduled",
    # In-process EventBus
    "EventBus",
    "EventHandler",
    # Configuration
    "ModuleHostConfig",
    # Middleware
    "Middleware",
    "NextCall",
    # Exceptions
    "PyModulesError",
    "PyModulesSignal",
    "CommandHandlingError",
    "ModuleRegistrationError",
    "DuplicateCommandError",
    "UnknownCommandError",
    "ConfigurationError",
    "DatabaseError",
    "ConnectionError",
    "RepositoryError",
    "RateLimitExceeded",
    "CircuitBreakerOpen",
    "SyncDispatchOnAsyncHandlerError",
    "SyncDispatchInAsyncContextError",
    "AgentError",
    "AgentFailed",
    "AgentNotRegistered",
    "AgentRunStuck",
    "AgentSpawnRejected",
    # Logging
    "configure_logging",
    "get_logger",
    # Resilience middleware
    "RateLimitMiddleware",
    "CircuitBreaker",
    "CircuitBreakerMiddleware",
    "CircuitState",
    "RetryPolicy",
    "RetryMiddleware",
    "DeadLetterQueue",
    "DeadLetterEntry",
    "DLQMiddleware",
    "Fallback",
    "FallbackMiddleware",
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "default_middleware",
    "default_middleware_from_env",
    # Tracing / observability middleware
    "Tracer",
    "TraceContext",
    "Span",
    "TracingMiddleware",
    "MetricsMiddleware",
    "LifecycleMiddleware",
    "get_tracer",
    "set_tracer",
    "get_current_trace",
    "get_correlation_id",
    "inject_trace_context",
    "extract_trace_context",
    "generate_id",
]

__version__ = "0.3.0"
