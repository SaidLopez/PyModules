"""
Fallback handler and middleware.

``Fallback`` is the stateless dataclass form used as a decorator on arbitrary
callables. ``FallbackMiddleware`` integrates the same concept into the
dispatch chain.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from ..exceptions import PyModulesSignal
from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

resilience_logger = get_logger("resilience")


@dataclass
class Fallback:
    """
    Decorator-form fallback for arbitrary callables.

    Returns ``default_value`` (or the result of ``fallback_func``) when the
    wrapped callable raises a matching exception.
    """

    default_value: Any = None
    fallback_func: Callable[[], Any] | None = None
    exceptions: tuple[type[Exception], ...] = field(default_factory=lambda: (Exception,))
    log_errors: bool = True

    def get_fallback(self) -> Any:
        if self.fallback_func:
            return self.fallback_func()
        return self.default_value

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except PyModulesSignal:
                raise
            except self.exceptions as e:
                if self.log_errors:
                    resilience_logger.warning("Fallback triggered for %s: %s", func.__name__, e)
                return self.get_fallback()

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except PyModulesSignal:
                raise
            except self.exceptions as e:
                if self.log_errors:
                    resilience_logger.warning("Fallback triggered for %s: %s", func.__name__, e)
                return self.get_fallback()

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper


class FallbackMiddleware:
    """
    Middleware returning a fallback value when the inner chain raises.

    Stateless apart from a counter exposed for observability.
    """

    def __init__(
        self,
        *,
        default_value: Any = None,
        fallback_func: Callable[[], Any] | None = None,
        exceptions: tuple[type[Exception], ...] = (Exception,),
        log_errors: bool = True,
    ) -> None:
        self.default_value = default_value
        self.fallback_func = fallback_func
        self.exceptions = exceptions
        self.log_errors = log_errors
        self.fallback_count = 0

    def _resolve(self) -> Any:
        if self.fallback_func:
            return self.fallback_func()
        return self.default_value

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        try:
            return await next_call(command)
        except PyModulesSignal:
            # Framework signals are control-flow markers, not the
            # downstream-failure cases a fallback is meant to mask.
            # Substituting a fallback for ``RateLimitExceeded`` would
            # silently defeat the rate limit; for ``UnknownCommandError``
            # it would mask a misrouted dispatch. Propagate untouched.
            raise
        except self.exceptions as e:
            if self.log_errors:
                resilience_logger.warning("Fallback triggered for command %s: %s", command.name, e)
            self.fallback_count += 1
            return self._resolve()


__all__ = ["Fallback", "FallbackMiddleware"]
