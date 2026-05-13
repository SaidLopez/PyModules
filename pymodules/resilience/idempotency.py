"""
Idempotency middleware.

Suppresses duplicate dispatches by ``Command.command_id``: a second
dispatch with the same id (within the store's retention window) returns
the cached response without re-invoking the handler. Concurrent dispatches
with the same id are serialised so the handler runs exactly once.

Only **successful** responses are cached. Exceptions propagate uncached
so a transient failure does not become a permanent error for the entire
TTL window — a subsequent dispatch with the same id re-runs the handler.

The store is pluggable via the ``IdempotencyStore`` protocol; contrib
packages can supply Redis/SQL backends. The bundled
``InMemoryIdempotencyStore`` is TTL-bounded and safe as the default.

Composition note: idempotency is the outermost middleware in the standard
chain. A cached hit returns before rate-limit tokens are consumed,
breaker state is touched, or retry runs — duplicate work is not "work".
"""

import asyncio
import threading
import time
import weakref
from typing import Any, Protocol, runtime_checkable

from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

resilience_logger = get_logger("resilience")


@runtime_checkable
class IdempotencyStore(Protocol):
    """Async cache from ``command_id`` → cached response value."""

    async def get(self, key: str) -> tuple[bool, Any]:
        """Return ``(hit, value)``; ``hit=False`` means miss."""
        ...

    async def put(self, key: str, value: Any) -> None:
        """Cache ``value`` under ``key`` for the store's retention window."""
        ...


class InMemoryIdempotencyStore:
    """
    Thread-safe TTL-bounded in-memory idempotency cache.

    Entries are evicted lazily on access once their TTL has elapsed. No
    background reaper — a key that is never re-read stays resident until
    it is next looked up or ``clear()`` is called.

    Attributes:
        ttl_seconds: Entries older than this are treated as misses and
            evicted on the next ``get``.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    async def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._entries[key]
                return False, None
            return True, value

    async def put(self, key: str, value: Any) -> None:
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._entries[key] = (value, expires_at)

    def clear(self) -> None:
        """Drop all entries. Mostly useful in tests."""
        with self._lock:
            self._entries.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)


class IdempotencyMiddleware:
    """
    Suppress duplicate dispatches by ``Command.command_id``.

    Commands with ``command_id=None`` pass through unchanged. Commands
    with a non-None id:

    - First call: handler runs; the successful response is cached; the
      cached value is returned.
    - Subsequent call (same id, within TTL): the cached response is
      returned without invoking the handler.
    - Concurrent calls (same id): serialised behind a per-key lock so the
      handler runs exactly once; later callers receive the cached
      response.

    Only successful responses are cached. Exceptions propagate uncached.

    The per-key lock dictionary uses weak references so locks are
    garbage-collected once no caller is holding them — the middleware's
    memory footprint stays bounded by concurrent (not historical) ids.

    Attributes:
        store: Backing ``IdempotencyStore``.
        hits: Number of dispatches served from the cache.
        misses: Number of dispatches that ran the handler.
        skipped: Number of dispatches with ``command_id=None``.
    """

    def __init__(self, store: IdempotencyStore | None = None) -> None:
        self.store: IdempotencyStore = store or InMemoryIdempotencyStore()
        self.hits = 0
        self.misses = 0
        self.skipped = 0
        self._key_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def _lock_for(self, key: str) -> asyncio.Lock:
        # Same-event-loop assumption: coroutines do not preempt between
        # non-await statements, so the get/create sequence is atomic.
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        key = command.command_id
        if key is None:
            self.skipped += 1
            return await next_call(command)

        async with self._lock_for(key):
            hit, cached = await self.store.get(key)
            if hit:
                self.hits += 1
                resilience_logger.debug(
                    "Idempotency hit for command %s id=%s", command.name, key
                )
                return cached

            self.misses += 1
            result = await next_call(command)
            await self.store.put(key, result)
            return result


__all__ = [
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
]
