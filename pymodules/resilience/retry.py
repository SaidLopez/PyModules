"""
Retry policy and middleware.

``RetryPolicy`` is the stateless config dataclass (max retries, base delay,
backoff). ``RetryMiddleware`` wraps the dispatch chain with the retry loop.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

resilience_logger = get_logger("resilience")


@dataclass
class RetryPolicy:
    """
    Retry policy with exponential backoff.

    Attributes:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        exponential_base: Base for exponential backoff.
        retryable_exceptions: Exception types eligible for retry.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    retryable_exceptions: tuple[type[Exception], ...] = field(default_factory=lambda: (Exception,))

    def calculate_delay(self, attempt: int) -> float:
        delay = self.base_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)

    def should_retry(self, exception: Exception, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        return isinstance(exception, self.retryable_exceptions)

    def __call__(self, func: Callable) -> Callable:
        """Decorator form."""

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if not self.should_retry(e, attempt):
                        raise
                    delay = self.calculate_delay(attempt)
                    resilience_logger.warning(
                        "Retry attempt %d/%d after %.2fs: %s",
                        attempt + 1,
                        self.max_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry state")

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if not self.should_retry(e, attempt):
                        raise
                    delay = self.calculate_delay(attempt)
                    resilience_logger.warning(
                        "Retry attempt %d/%d after %.2fs: %s",
                        attempt + 1,
                        self.max_retries,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry state")

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper


class RetryMiddleware:
    """
    Retry the next-call until it succeeds, ``max_retries`` is reached, or
    the exception is non-retryable.

    Attributes:
        policy: The ``RetryPolicy`` consulted on each failure.
        retry_count: Number of retry attempts performed (not commands).
    """

    def __init__(self, policy: RetryPolicy) -> None:
        self.policy = policy
        self.retry_count = 0

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        attempt = 0
        while True:
            try:
                return await next_call(command)
            except Exception as e:
                if not self.policy.should_retry(e, attempt):
                    raise
                self.retry_count += 1
                delay = self.policy.calculate_delay(attempt)
                resilience_logger.warning(
                    "Retrying command %s (attempt %d) after %.2fs: %s",
                    command.name,
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                attempt += 1


__all__ = ["RetryMiddleware", "RetryPolicy"]
