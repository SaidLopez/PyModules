"""
Configuration management for PyModules framework.

``ModuleHostConfig`` now carries only:

  - ``max_workers`` / ``propagate_exceptions`` / ``log_level`` — generic
    host settings.
  - ``middleware`` — the ordered list that drives the dispatch chain.

Resilience flags (``rate_limiter=``, ``circuit_breaker=``, ``retry_policy=``,
``dead_letter_queue=``, ``tracer=``, ``enable_metrics``, ``enable_tracing``)
were deleted in the 1.0 migration; the lifecycle callbacks
(``on_event_start``, ``on_event_end``, ``on_error``) too. Build the
middleware list explicitly or via
``pymodules.resilience.default_middleware(...)``.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .middleware import Middleware


@dataclass
class ModuleHostConfig:
    """
    Configuration for ``ModuleHost``.

    Attributes:
        max_workers: Maximum threads in the executor pool used to run sync
            handlers from ``dispatch_async``.
        propagate_exceptions: If True, exceptions from the inner chain are
            wrapped in ``CommandHandlingError`` and re-raised by the host.
        log_level: Logging level for the framework.
        middleware: Ordered list of middleware. The first entry is the
            outermost wrapper; the terminal handler-lookup middleware is
            appended by the host itself and is not part of this list.
    """

    max_workers: int = 4
    propagate_exceptions: bool = True
    log_level: int = logging.INFO
    middleware: list["Middleware"] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "ModuleHostConfig":
        """
        Create configuration from environment variables.

        Only host-level settings are read here. Resilience and tracing
        env vars are owned by ``pymodules.resilience.default_middleware_from_env``
        and ``pymodules.tracing.middleware_from_env``.

        Environment variables:
            PYMODULES_MAX_WORKERS: Max thread pool workers (default: 4).
            PYMODULES_PROPAGATE_EXCEPTIONS: "true"/"false" (default: true).
            PYMODULES_LOG_LEVEL: DEBUG/INFO/WARNING/ERROR (default: INFO).
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

        return cls(
            max_workers=max_workers,
            propagate_exceptions=propagate_str == "true",
            log_level=log_level_map.get(log_level_str, logging.INFO),
        )


__all__ = ["ModuleHostConfig"]
