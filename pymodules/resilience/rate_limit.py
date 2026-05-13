"""
Rate limiting middleware.

``RateLimitMiddleware`` owns its token-bucket state inline — there is no
separate ``RateLimiter`` class. The rejected-request counter is exposed as
an attribute on the middleware instance so user code can hold a reference.
"""

import asyncio
import threading
import time
from typing import Any

from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

resilience_logger = get_logger("resilience")


class RateLimitExceeded(Exception):
    """Raised when the rate limit is exceeded and ``block=False``."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitMiddleware:
    """
    Token-bucket rate limiter as a middleware.

    Owns its token-bucket state directly. Use ``block=True`` to wait for
    tokens to become available; otherwise rejects with ``RateLimitExceeded``.

    Attributes (read-only after construction):
        rate: Maximum commands per second.
        burst: Maximum burst size (bucket capacity).
        block: If True, wait for tokens; if False, raise on exhaustion.
        rejected_count: Number of commands rejected for being over the limit.
    """

    def __init__(self, *, rate: float = 100.0, burst: int = 10, block: bool = False) -> None:
        self.rate = rate
        self.burst = burst
        self.block = block
        self._tokens: float = float(burst)
        self._last_update: float = time.monotonic()
        self._lock = threading.Lock()
        self.rejected_count = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_update = now

    def _try_acquire(self, tokens: int = 1) -> tuple[bool, float]:
        """Return (acquired, wait_or_retry_after)."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0
            return False, (tokens - self._tokens) / self.rate

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        acquired, wait = self._try_acquire(1)
        if not acquired:
            if self.block:
                await asyncio.sleep(wait)
                # After waiting we re-attempt; tokens should be available.
                acquired, _ = self._try_acquire(1)
                if not acquired:
                    self.rejected_count += 1
                    resilience_logger.warning(
                        "Rate limit exceeded for command %s after wait", command.name
                    )
                    raise RateLimitExceeded(
                        f"Rate limit exceeded. Retry after {wait:.2f}s",
                        retry_after=wait,
                    )
            else:
                self.rejected_count += 1
                resilience_logger.warning("Rate limit exceeded for command %s", command.name)
                raise RateLimitExceeded(
                    f"Rate limit exceeded. Retry after {wait:.2f}s",
                    retry_after=wait,
                )
        return await next_call(command)

    def reset(self) -> None:
        """Reset the bucket to full capacity (does not touch ``rejected_count``)."""
        with self._lock:
            self._tokens = float(self.burst)
            self._last_update = time.monotonic()


__all__ = ["RateLimitExceeded", "RateLimitMiddleware"]
