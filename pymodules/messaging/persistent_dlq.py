"""
Persistent Dead Letter Queue backed by Redis.

Extends the in-memory DeadLetterQueue with Redis persistence for durability.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from ..resilience import DeadLetterEntry

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis.asyncio as redis_async

    from ..interfaces import Event

logger = get_logger("messaging.dlq")


# =============================================================================
# Persistent DLQ Configuration
# =============================================================================


@dataclass
class PersistentDLQConfig:
    """
    Configuration for persistent dead letter queue.

    Attributes:
        redis_url: Redis connection URL.
        key_prefix: Prefix for DLQ keys.
        max_size: Maximum entries in the queue.
        ttl: Time-to-live in seconds for DLQ entries (0 = no expiry).
        batch_size: Batch size for bulk operations.

    Environment Variables:
        PYMODULES_DLQ_REDIS_URL
        PYMODULES_DLQ_KEY_PREFIX
        PYMODULES_DLQ_MAX_SIZE
        PYMODULES_DLQ_TTL
    """

    redis_url: str = "redis://localhost:6379/0"
    key_prefix: str = "pymodules:dlq"
    max_size: int = 10000
    ttl: int = 0  # seconds, 0 = no expiry
    batch_size: int = 100

    @classmethod
    def from_env(cls) -> PersistentDLQConfig:
        """Load configuration from environment variables."""
        import os

        return cls(
            redis_url=os.getenv("PYMODULES_DLQ_REDIS_URL", "redis://localhost:6379/0"),
            key_prefix=os.getenv("PYMODULES_DLQ_KEY_PREFIX", "pymodules:dlq"),
            max_size=int(os.getenv("PYMODULES_DLQ_MAX_SIZE", "10000")),
            ttl=int(os.getenv("PYMODULES_DLQ_TTL", "0")),
        )


# =============================================================================
# Serialization Helpers
# =============================================================================


def serialize_entry(entry: DeadLetterEntry) -> dict[str, Any]:
    """Serialize a DeadLetterEntry to a dictionary."""
    return {
        "event": {
            "name": entry.event.name,
            "input": entry.event.input.__dict__ if entry.event.input else None,
            "output": entry.event.output.__dict__ if entry.event.output else None,
            "handled": entry.event.handled,
            "meta": entry.event.meta,
        },
        "error": str(entry.error),
        "error_type": type(entry.error).__name__,
        "timestamp": entry.timestamp,
        "attempts": entry.attempts,
        "module_name": entry.module_name,
    }


def deserialize_entry(data: dict[str, Any]) -> DeadLetterEntry:
    """Deserialize a dictionary to a DeadLetterEntry."""
    from ..interfaces import Event, EventInput, EventOutput

    event_data = data["event"]

    # Reconstruct input/output
    input_data = event_data.get("input")
    output_data = event_data.get("output")

    input_obj = None
    if input_data:
        input_obj = EventInput()
        for k, v in input_data.items():
            setattr(input_obj, k, v)

    output_obj = None
    if output_data:
        output_obj = EventOutput()
        for k, v in output_data.items():
            setattr(output_obj, k, v)

    event = Event(
        name=event_data["name"],
        input=input_obj,
        output=output_obj,
        handled=event_data.get("handled", False),
        meta=event_data.get("meta", {}),
    )

    return DeadLetterEntry(
        event=event,
        error=Exception(data["error"]),
        timestamp=data["timestamp"],
        attempts=data["attempts"],
        module_name=data.get("module_name", ""),
    )


# =============================================================================
# Persistent Dead Letter Queue
# =============================================================================


class PersistentDeadLetterQueue:
    """
    Redis-backed persistent dead letter queue.

    Stores failed events in Redis for durability and cross-service access.
    Supports async operations for high-throughput scenarios.

    Example:
        dlq = PersistentDeadLetterQueue(config)
        await dlq.connect()

        # Add failed event
        await dlq.add(event, error, module_name="my_module")

        # Inspect entries
        entries = await dlq.get_entries(limit=10)
        for entry in entries:
            print(f"Failed: {entry.event.name} - {entry.error}")

        # Reprocess
        success, failed = await dlq.reprocess(host.handle_async)

        await dlq.disconnect()
    """

    def __init__(
        self,
        config: PersistentDLQConfig | None = None,
        on_add: Callable[[DeadLetterEntry], None] | None = None,
    ):
        """
        Initialize persistent DLQ.

        Args:
            config: DLQ configuration.
            on_add: Optional callback when entry is added.
        """
        self.config = config or PersistentDLQConfig()
        self._on_add = on_add
        self._redis: redis_async.Redis | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Check if connected to Redis."""
        return self._connected

    @property
    def _list_key(self) -> str:
        """Redis key for the DLQ list."""
        return f"{self.config.key_prefix}:entries"

    @property
    def _counter_key(self) -> str:
        """Redis key for entry counter."""
        return f"{self.config.key_prefix}:counter"

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._connected:
            return

        try:
            import redis.asyncio as redis_async
        except ImportError as e:
            raise RuntimeError(
                "redis package not installed. Install with: pip install pymodules[redis]"
            ) from e

        self._redis = redis_async.from_url(
            self.config.redis_url,
            decode_responses=True,
        )

        await self._redis.ping()
        self._connected = True
        logger.info("Persistent DLQ connected to Redis")

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None
        self._connected = False
        logger.info("Persistent DLQ disconnected")

    async def __aenter__(self) -> PersistentDeadLetterQueue:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type | None, exc_val: Exception | None, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.disconnect()

    # -------------------------------------------------------------------------
    # Core Operations
    # -------------------------------------------------------------------------

    async def add(
        self,
        event: Event[Any, Any],
        error: Exception,
        module_name: str = "",
        attempts: int = 1,
    ) -> DeadLetterEntry:
        """
        Add a failed event to the DLQ.

        Args:
            event: The event that failed.
            error: The exception that caused the failure.
            module_name: Name of the module that was processing.
            attempts: Number of attempts made.

        Returns:
            The created DeadLetterEntry.
        """
        if not self._redis:
            raise RuntimeError("Not connected to Redis")

        entry = DeadLetterEntry(
            event=event,
            error=error,
            module_name=module_name,
            attempts=attempts,
        )

        serialized = json.dumps(serialize_entry(entry))

        async with self._redis.pipeline(transaction=True) as pipe:
            # Add to list
            pipe.lpush(self._list_key, serialized)

            # Trim to max size
            pipe.ltrim(self._list_key, 0, self.config.max_size - 1)

            # Increment counter
            pipe.incr(self._counter_key)

            # Set TTL if configured
            if self.config.ttl > 0:
                pipe.expire(self._list_key, self.config.ttl)

            await pipe.execute()

        logger.warning(
            "Event %s added to persistent DLQ after %d attempts: %s",
            event.name,
            attempts,
            error,
        )

        if self._on_add:
            try:
                self._on_add(entry)
            except Exception as e:
                logger.error("DLQ on_add callback failed: %s", e)

        return entry

    async def get_entries(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> list[DeadLetterEntry]:
        """
        Get entries from the DLQ.

        Args:
            offset: Starting index.
            limit: Maximum entries to return.

        Returns:
            List of DeadLetterEntry objects.
        """
        if not self._redis:
            return []

        entries_json = await self._redis.lrange(self._list_key, offset, offset + limit - 1)
        return [deserialize_entry(json.loads(entry)) for entry in entries_json]

    async def pop(self) -> DeadLetterEntry | None:
        """
        Remove and return the oldest entry.

        Returns:
            The oldest entry, or None if empty.
        """
        if not self._redis:
            return None

        entry_json = await self._redis.rpop(self._list_key)
        if entry_json:
            return deserialize_entry(json.loads(entry_json))
        return None

    async def __len__(self) -> int:
        """Get number of entries in the queue."""
        return await self.length()

    async def length(self) -> int:
        """Get number of entries in the queue."""
        if not self._redis:
            return 0
        result = await self._redis.llen(self._list_key)
        return int(result)

    async def clear(self) -> int:
        """
        Clear all entries.

        Returns:
            Number of entries cleared.
        """
        if not self._redis:
            return 0

        count = await self._redis.llen(self._list_key)
        await self._redis.delete(self._list_key)
        return int(count)

    # -------------------------------------------------------------------------
    # Reprocessing
    # -------------------------------------------------------------------------

    async def reprocess(
        self,
        handler: Callable[[Event[Any, Any]], Any],
        max_entries: int | None = None,
        batch_size: int | None = None,
    ) -> tuple[int, int]:
        """
        Reprocess entries from the DLQ.

        Args:
            handler: Async function to handle events (e.g., host.handle_async).
            max_entries: Maximum entries to process (None = all).
            batch_size: Batch size for processing.

        Returns:
            Tuple of (successful, failed) counts.
        """
        if not self._redis:
            return (0, 0)

        batch_size = batch_size or self.config.batch_size
        successful = 0
        failed = 0
        processed = 0

        while True:
            if max_entries and processed >= max_entries:
                break

            entry = await self.pop()
            if entry is None:
                break

            processed += 1

            try:
                # Reset event state
                entry.event.handled = False
                entry.event.output = None

                # Handle event (sync or async)
                result = handler(entry.event)
                if asyncio.iscoroutine(result):
                    await result

                if entry.event.handled:
                    successful += 1
                    logger.info(
                        "Successfully reprocessed event %s from DLQ",
                        entry.event.name,
                    )
                else:
                    # No handler found, re-add
                    await self.add(
                        entry.event,
                        RuntimeError("No handler found on reprocess"),
                        attempts=entry.attempts + 1,
                    )
                    failed += 1

            except Exception as e:
                # Failed again, re-add
                await self.add(
                    entry.event,
                    e,
                    module_name=entry.module_name,
                    attempts=entry.attempts + 1,
                )
                failed += 1

        return successful, failed

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """
        Get DLQ statistics.

        Returns:
            Dictionary with queue statistics.
        """
        if not self._redis:
            return {}

        length = await self._redis.llen(self._list_key)
        total = await self._redis.get(self._counter_key) or 0

        # Get recent entries for analysis
        recent_entries = await self.get_entries(limit=100)

        error_types: dict[str, int] = {}
        event_names: dict[str, int] = {}
        modules: dict[str, int] = {}

        for entry in recent_entries:
            error_type = type(entry.error).__name__
            error_types[error_type] = error_types.get(error_type, 0) + 1

            event_names[entry.event.name] = event_names.get(entry.event.name, 0) + 1

            if entry.module_name:
                modules[entry.module_name] = modules.get(entry.module_name, 0) + 1

        return {
            "current_length": length,
            "total_added": int(total),
            "max_size": self.config.max_size,
            "error_types": error_types,
            "event_names": event_names,
            "modules": modules,
        }

    # -------------------------------------------------------------------------
    # Integration with In-Memory DLQ
    # -------------------------------------------------------------------------

    async def sync_from_memory(
        self,
        memory_dlq: Any,  # DeadLetterQueue
    ) -> int:
        """
        Sync entries from an in-memory DLQ to persistent storage.

        Args:
            memory_dlq: In-memory DeadLetterQueue instance.

        Returns:
            Number of entries synced.
        """
        count = 0
        while True:
            entry = memory_dlq.pop()
            if entry is None:
                break

            await self.add(
                entry.event,
                entry.error,
                module_name=entry.module_name,
                attempts=entry.attempts,
            )
            count += 1

        logger.info("Synced %d entries from memory DLQ to persistent storage", count)
        return count
