"""
Configuration management for PyModules framework.

Provides a centralized configuration system with environment variable support.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .resilience import CircuitBreaker, DeadLetterQueue, RateLimiter, RetryPolicy
    from .tracing import Tracer


@dataclass
class ModuleHostConfig:
    """
    Configuration for ModuleHost.

    Attributes:
        max_workers: Maximum threads in the executor pool.
        propagate_exceptions: If True, exceptions from handlers are re-raised.
        log_level: Logging level for the framework.
        on_error: Optional callback for handling errors.
        on_event_start: Optional callback when command dispatch starts.
        on_event_end: Optional callback when command dispatch ends.
        enable_metrics: Enable basic metrics collection.
        enable_tracing: Enable distributed tracing.
        rate_limiter: Optional rate limiter for command throttling.
        circuit_breaker: Optional circuit breaker for fault tolerance.
        retry_policy: Optional retry policy for failed commands.
        dead_letter_queue: Optional DLQ for failed commands.
        tracer: Optional tracer for distributed tracing.
    """

    max_workers: int = 4
    propagate_exceptions: bool = True
    log_level: int = logging.INFO
    on_error: Callable[[Exception, Any], None] | None = None
    on_event_start: Callable[[Any], None] | None = None
    on_event_end: Callable[[Any, bool], None] | None = None
    enable_metrics: bool = False
    enable_tracing: bool = False

    # Resilience features (set to instances to enable)
    rate_limiter: "RateLimiter | None" = None
    circuit_breaker: "CircuitBreaker | None" = None
    retry_policy: "RetryPolicy | None" = None
    dead_letter_queue: "DeadLetterQueue | None" = None

    # Tracing
    tracer: "Tracer | None" = None

    @classmethod
    def from_env(cls) -> "ModuleHostConfig":
        """
        Create configuration from environment variables.

        Environment variables:
            PYMODULES_MAX_WORKERS: Max thread pool workers (default: 4)
            PYMODULES_PROPAGATE_EXCEPTIONS: "true"/"false" (default: true)
            PYMODULES_LOG_LEVEL: DEBUG/INFO/WARNING/ERROR (default: INFO)
            PYMODULES_ENABLE_METRICS: "true"/"false" (default: false)
            PYMODULES_ENABLE_TRACING: "true"/"false" (default: false)
            PYMODULES_RATE_LIMIT: Commands per second, 0 to disable (default: 0)
            PYMODULES_RATE_LIMIT_BURST: Burst size for rate limiter (default: 10)
            PYMODULES_CIRCUIT_BREAKER_THRESHOLD: Failures before open (default: 0, disabled)
            PYMODULES_CIRCUIT_BREAKER_TIMEOUT: Recovery timeout seconds (default: 30)
            PYMODULES_RETRY_MAX: Max retry attempts, 0 to disable (default: 0)
            PYMODULES_RETRY_BASE_DELAY: Base delay in seconds (default: 1.0)
            PYMODULES_DLQ_SIZE: Max DLQ entries, 0 to disable (default: 0)

        Returns:
            ModuleHostConfig instance.

        Example:
            import os
            os.environ["PYMODULES_MAX_WORKERS"] = "8"
            os.environ["PYMODULES_LOG_LEVEL"] = "DEBUG"
            os.environ["PYMODULES_RATE_LIMIT"] = "100"

            config = ModuleHostConfig.from_env()
        """
        log_level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }

        max_workers = int(os.getenv("PYMODULES_MAX_WORKERS", "4"))
        propagate_str = os.getenv("PYMODULES_PROPAGATE_EXCEPTIONS", "true").lower()
        log_level_str = os.getenv("PYMODULES_LOG_LEVEL", "INFO").upper()
        metrics_str = os.getenv("PYMODULES_ENABLE_METRICS", "false").lower()
        tracing_str = os.getenv("PYMODULES_ENABLE_TRACING", "false").lower()

        # Rate limiter config
        rate_limit = float(os.getenv("PYMODULES_RATE_LIMIT", "0"))
        rate_limit_burst = int(os.getenv("PYMODULES_RATE_LIMIT_BURST", "10"))

        # Circuit breaker config
        cb_threshold = int(os.getenv("PYMODULES_CIRCUIT_BREAKER_THRESHOLD", "0"))
        cb_timeout = float(os.getenv("PYMODULES_CIRCUIT_BREAKER_TIMEOUT", "30"))

        # Retry config
        retry_max = int(os.getenv("PYMODULES_RETRY_MAX", "0"))
        retry_base_delay = float(os.getenv("PYMODULES_RETRY_BASE_DELAY", "1.0"))

        # DLQ config
        dlq_size = int(os.getenv("PYMODULES_DLQ_SIZE", "0"))

        # Create resilience components if configured
        rate_limiter = None
        circuit_breaker = None
        retry_policy = None
        dead_letter_queue = None
        tracer = None

        if rate_limit > 0:
            from .resilience import RateLimiter

            rate_limiter = RateLimiter(rate=rate_limit, burst=rate_limit_burst)

        if cb_threshold > 0:
            from .resilience import CircuitBreaker

            circuit_breaker = CircuitBreaker(
                failure_threshold=cb_threshold,
                recovery_timeout=cb_timeout,
            )

        if retry_max > 0:
            from .resilience import RetryPolicy

            retry_policy = RetryPolicy(
                max_retries=retry_max,
                base_delay=retry_base_delay,
            )

        if dlq_size > 0:
            from .resilience import DeadLetterQueue

            dead_letter_queue = DeadLetterQueue(max_size=dlq_size)

        if tracing_str == "true":
            from .tracing import Tracer

            tracer = Tracer()

        return cls(
            max_workers=max_workers,
            propagate_exceptions=propagate_str == "true",
            log_level=log_level_map.get(log_level_str, logging.INFO),
            enable_metrics=metrics_str == "true",
            enable_tracing=tracing_str == "true",
            rate_limiter=rate_limiter,
            circuit_breaker=circuit_breaker,
            retry_policy=retry_policy,
            dead_letter_queue=dead_letter_queue,
            tracer=tracer,
        )


@dataclass
class Metrics:
    """
    Simple metrics collection for ModuleHost.

    Attributes:
        events_dispatched: Total number of events dispatched.
        events_handled: Number of events that were handled.
        events_unhandled: Number of events with no handler.
        events_failed: Number of events that raised exceptions.
        events_retried: Number of events that were retried.
        events_rate_limited: Number of events rejected by rate limiter.
        events_circuit_broken: Number of events rejected by circuit breaker.
        events_dead_lettered: Number of events sent to DLQ.
        modules_registered: Current number of registered modules.
    """

    events_dispatched: int = 0
    events_handled: int = 0
    events_unhandled: int = 0
    events_failed: int = 0
    events_retried: int = 0
    events_rate_limited: int = 0
    events_circuit_broken: int = 0
    events_dead_lettered: int = 0
    modules_registered: int = 0

    def to_dict(self) -> dict:
        """Convert metrics to dictionary for JSON serialization."""
        return {
            "events_dispatched": self.events_dispatched,
            "events_handled": self.events_handled,
            "events_unhandled": self.events_unhandled,
            "events_failed": self.events_failed,
            "events_retried": self.events_retried,
            "events_rate_limited": self.events_rate_limited,
            "events_circuit_broken": self.events_circuit_broken,
            "events_dead_lettered": self.events_dead_lettered,
            "modules_registered": self.modules_registered,
        }

    def reset(self) -> None:
        """Reset all metrics to zero."""
        self.events_dispatched = 0
        self.events_handled = 0
        self.events_unhandled = 0
        self.events_failed = 0
        self.events_retried = 0
        self.events_rate_limited = 0
        self.events_circuit_broken = 0
        self.events_dead_lettered = 0
