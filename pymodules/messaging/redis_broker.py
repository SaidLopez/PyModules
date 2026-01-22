"""
Redis Streams implementation of MessageBroker.

Uses Redis Streams with consumer groups for reliable message delivery.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..logging import get_logger
from .broker import (
    BrokerConnectionError,
    ConsumeError,
    Message,
    MessageBroker,
    MessageBrokerConfig,
    PublishError,
)

if TYPE_CHECKING:
    import redis.asyncio as redis_async

logger = get_logger("messaging.redis")


# =============================================================================
# Redis-Specific Configuration
# =============================================================================


@dataclass
class RedisBrokerConfig(MessageBrokerConfig):
    """
    Redis-specific broker configuration.

    Attributes:
        url: Redis connection URL.
        max_connections: Maximum connections in pool.
        socket_timeout: Socket timeout in seconds.
        retry_on_timeout: Retry on timeout errors.
        decode_responses: Decode string responses.

    Environment Variables:
        PYMODULES_REDIS_URL
        PYMODULES_REDIS_MAX_CONNECTIONS
        PYMODULES_REDIS_SOCKET_TIMEOUT
    """

    url: str = "redis://localhost:6379/0"
    max_connections: int = 10
    socket_timeout: float = 5.0
    retry_on_timeout: bool = True
    decode_responses: bool = True

    @classmethod
    def from_env(cls) -> RedisBrokerConfig:
        """Load configuration from environment variables."""
        import os

        # Get base config
        base = MessageBrokerConfig.from_env()

        return cls(
            # Base config
            stream_prefix=base.stream_prefix,
            consumer_group=base.consumer_group,
            consumer_name=base.consumer_name,
            max_retries=base.max_retries,
            ack_timeout=base.ack_timeout,
            batch_size=base.batch_size,
            block_timeout=base.block_timeout,
            # Redis-specific
            url=os.getenv("PYMODULES_REDIS_URL", "redis://localhost:6379/0"),
            max_connections=int(os.getenv("PYMODULES_REDIS_MAX_CONNECTIONS", "10")),
            socket_timeout=float(os.getenv("PYMODULES_REDIS_SOCKET_TIMEOUT", "5.0")),
        )


# =============================================================================
# Redis Broker Implementation
# =============================================================================


@dataclass
class PendingMessage:
    """Tracks a message that's being processed."""

    message: Message
    delivery_count: int = 1
    first_delivered: float = 0.0


class RedisBroker(MessageBroker):
    """
    Redis Streams message broker implementation.

    Features:
    - Consumer groups for load balancing
    - Automatic stream and group creation
    - Message acknowledgment tracking
    - Pending message recovery
    - Dead letter queue support

    Example:
        config = RedisBrokerConfig(url="redis://localhost:6379")
        broker = RedisBroker(config)

        async with broker:
            # Publish
            await broker.publish("events", Message(data={"user": "john"}))

            # Subscribe and consume
            await broker.subscribe(["events"])
            async for msg in broker.consume():
                print(msg.data)
                await broker.ack(msg)
    """

    config: RedisBrokerConfig = field(default_factory=RedisBrokerConfig)

    def __init__(self, config: RedisBrokerConfig | None = None, url: str | None = None):
        """
        Initialize Redis broker.

        Args:
            config: Redis broker configuration.
            url: Redis URL (shortcut, overrides config.url if provided).
        """
        if config is None:
            config = RedisBrokerConfig()
        if url is not None:
            config.url = url

        super().__init__(config)
        self.config: RedisBrokerConfig = config  # Type hint for IDE

        self._redis: redis_async.Redis | None = None
        self._subscribed_streams: list[str] = []
        self._pending: dict[str, PendingMessage] = {}
        self._running = False

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
            raise BrokerConnectionError(
                "redis package not installed. Install with: pip install pymodules[redis]"
            ) from e

        try:
            self._redis = redis_async.from_url(
                self.config.url,
                max_connections=self.config.max_connections,
                socket_timeout=self.config.socket_timeout,
                retry_on_timeout=self.config.retry_on_timeout,
                decode_responses=self.config.decode_responses,
            )

            # Test connection
            await self._redis.ping()
            self._connected = True
            logger.info("Connected to Redis at %s", self.config.url.split("@")[-1])

            if hasattr(self, "_on_connect"):
                self._on_connect()

        except Exception as e:
            raise BrokerConnectionError(f"Failed to connect to Redis: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        self._running = False

        if self._redis:
            await self._redis.close()
            self._redis = None

        self._connected = False
        self._subscribed_streams = []
        logger.info("Disconnected from Redis")

        if hasattr(self, "_on_disconnect"):
            self._on_disconnect()

    # -------------------------------------------------------------------------
    # Publishing
    # -------------------------------------------------------------------------

    async def publish(self, stream: str, message: Message) -> str:
        """Publish a message to a Redis stream."""
        if not self._redis:
            raise PublishError("Not connected to Redis")

        full_stream = self._full_stream_name(stream)
        message.stream = full_stream

        try:
            # Serialize message to Redis hash fields
            fields = {
                "id": message.id,
                "data": json.dumps(message.data),
                "headers": json.dumps(message.headers),
                "timestamp": str(message.timestamp),
                "event_name": message.event_name,
            }

            # Add to stream
            message_id = await self._redis.xadd(full_stream, fields)
            logger.debug("Published message %s to stream %s", message_id, full_stream)
            return str(message_id)

        except Exception as e:
            if hasattr(self, "_on_error"):
                self._on_error(e)
            raise PublishError(f"Failed to publish to {stream}: {e}") from e

    async def publish_batch(self, stream: str, messages: list[Message]) -> list[str]:
        """Publish multiple messages using pipeline."""
        if not self._redis:
            raise PublishError("Not connected to Redis")

        full_stream = self._full_stream_name(stream)
        message_ids: list[str] = []

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for message in messages:
                    message.stream = full_stream
                    fields = {
                        "id": message.id,
                        "data": json.dumps(message.data),
                        "headers": json.dumps(message.headers),
                        "timestamp": str(message.timestamp),
                        "event_name": message.event_name,
                    }
                    pipe.xadd(full_stream, fields)

                results = await pipe.execute()
                message_ids = [str(r) for r in results]

            logger.debug("Published %d messages to stream %s", len(messages), full_stream)
            return message_ids

        except Exception as e:
            raise PublishError(f"Failed to publish batch to {stream}: {e}") from e

    # -------------------------------------------------------------------------
    # Consuming
    # -------------------------------------------------------------------------

    async def subscribe(self, streams: list[str]) -> None:
        """Subscribe to streams by creating consumer groups."""
        if not self._redis:
            raise ConsumeError("Not connected to Redis")

        for stream in streams:
            full_stream = self._full_stream_name(stream)

            try:
                # Create consumer group (starts from latest message)
                # Use MKSTREAM to create stream if it doesn't exist
                await self._redis.xgroup_create(
                    full_stream,
                    self.config.consumer_group,
                    id="$",
                    mkstream=True,
                )
                logger.info(
                    "Created consumer group %s for stream %s",
                    self.config.consumer_group,
                    full_stream,
                )
            except Exception as e:
                # Group already exists is OK
                if "BUSYGROUP" not in str(e):
                    raise ConsumeError(f"Failed to create consumer group: {e}") from e
                logger.debug("Consumer group %s already exists", self.config.consumer_group)

            if full_stream not in self._subscribed_streams:
                self._subscribed_streams.append(full_stream)

    async def consume(self) -> AsyncIterator[Message]:
        """Consume messages from subscribed streams."""
        if not self._redis:
            raise ConsumeError("Not connected to Redis")

        if not self._subscribed_streams:
            raise ConsumeError("No streams subscribed")

        self._running = True

        # Build streams dict for XREADGROUP
        streams_dict = {stream: ">" for stream in self._subscribed_streams}

        while self._running:
            try:
                # Read new messages
                results = await self._redis.xreadgroup(
                    groupname=self.config.consumer_group,
                    consumername=self.config.consumer_name,
                    streams=streams_dict,
                    count=self.config.batch_size,
                    block=self.config.block_timeout,
                )

                if not results:
                    # Check for pending messages that timed out
                    await self._recover_pending()
                    continue

                for stream_name, messages in results:
                    for msg_id, fields in messages:
                        message = self._parse_message(stream_name, msg_id, fields)
                        self._pending[msg_id] = PendingMessage(
                            message=message,
                            first_delivered=message.timestamp,
                        )
                        yield message

            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                logger.error("Error consuming messages: %s", e)
                if hasattr(self, "_on_error"):
                    self._on_error(e)
                await asyncio.sleep(1)  # Brief pause before retry

    def _parse_message(
        self, stream: str, msg_id: str, fields: dict[str, str]
    ) -> Message:
        """Parse Redis stream entry into Message."""
        return Message(
            id=fields.get("id", msg_id),
            data=json.loads(fields.get("data", "{}")),
            headers=json.loads(fields.get("headers", "{}")),
            timestamp=float(fields.get("timestamp", "0")),
            event_name=fields.get("event_name", ""),
            stream=stream,
        )

    async def _recover_pending(self) -> None:
        """Recover messages that haven't been acknowledged."""
        if not self._redis:
            return

        for stream in self._subscribed_streams:
            try:
                # Check for pending messages older than ack_timeout
                pending = await self._redis.xpending_range(
                    stream,
                    self.config.consumer_group,
                    min="-",
                    max="+",
                    count=self.config.batch_size,
                )

                for entry in pending:
                    msg_id = entry["message_id"]
                    idle_time = entry["time_since_delivered"]
                    delivery_count = entry["times_delivered"]

                    # If message has been pending too long
                    if idle_time > self.config.ack_timeout * 1000:  # Convert to ms
                        if delivery_count >= self.config.max_retries:
                            # Move to DLQ
                            logger.warning(
                                "Message %s exceeded max retries, moving to DLQ", msg_id
                            )
                            await self._move_to_dlq(stream, msg_id)
                        else:
                            # Reclaim and redeliver
                            logger.debug("Reclaiming pending message %s", msg_id)
                            await self._redis.xclaim(
                                stream,
                                self.config.consumer_group,
                                self.config.consumer_name,
                                min_idle_time=int(self.config.ack_timeout * 1000),
                                message_ids=[msg_id],
                            )

            except Exception as e:
                logger.error("Error recovering pending messages: %s", e)

    async def _move_to_dlq(self, stream: str, msg_id: str) -> None:
        """Move a failed message to the dead letter queue."""
        if not self._redis:
            return

        dlq_stream = f"{stream}:dlq"

        try:
            # Read the message
            messages = await self._redis.xrange(stream, min=msg_id, max=msg_id, count=1)
            if messages:
                _, fields = messages[0]
                fields["original_stream"] = stream
                fields["original_id"] = msg_id
                fields["dlq_timestamp"] = str(asyncio.get_event_loop().time())

                # Add to DLQ
                await self._redis.xadd(dlq_stream, fields)

            # Acknowledge from original stream
            await self._redis.xack(stream, self.config.consumer_group, msg_id)

        except Exception as e:
            logger.error("Error moving message to DLQ: %s", e)

    # -------------------------------------------------------------------------
    # Acknowledgment
    # -------------------------------------------------------------------------

    async def ack(self, message: Message) -> None:
        """Acknowledge a message as processed."""
        if not self._redis:
            return

        try:
            await self._redis.xack(
                message.stream, self.config.consumer_group, message.id
            )
            self._pending.pop(message.id, None)
            logger.debug("Acknowledged message %s", message.id)
        except Exception as e:
            logger.error("Error acknowledging message %s: %s", message.id, e)

    async def nack(self, message: Message, requeue: bool = True) -> None:
        """Negatively acknowledge a message."""
        if not self._redis:
            return

        pending = self._pending.get(message.id)
        delivery_count = pending.delivery_count if pending else 1

        if not requeue or delivery_count >= self.config.max_retries:
            # Move to DLQ
            await self._move_to_dlq(message.stream, message.id)
        else:
            # Will be redelivered on next consume (leave unacknowledged)
            logger.debug("Message %s will be redelivered (attempt %d)", message.id, delivery_count)

        self._pending.pop(message.id, None)

    # -------------------------------------------------------------------------
    # Health & Info
    # -------------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check Redis connection health."""
        if not self._redis:
            return False

        try:
            await self._redis.ping()
            return True
        except Exception:
            return False

    async def stream_info(self, stream: str) -> dict[str, Any]:
        """Get information about a stream."""
        if not self._redis:
            return {}

        full_stream = self._full_stream_name(stream)

        try:
            info = await self._redis.xinfo_stream(full_stream)
            groups = await self._redis.xinfo_groups(full_stream)

            return {
                "length": info.get("length", 0),
                "first_entry": info.get("first-entry"),
                "last_entry": info.get("last-entry"),
                "groups": len(groups),
                "group_info": groups,
            }
        except Exception as e:
            logger.error("Error getting stream info: %s", e)
            return {"error": str(e)}

    # -------------------------------------------------------------------------
    # Additional Redis-Specific Methods
    # -------------------------------------------------------------------------

    async def trim_stream(
        self, stream: str, maxlen: int | None = None, minid: str | None = None
    ) -> int:
        """
        Trim a stream to a maximum length or minimum ID.

        Args:
            stream: Stream name.
            maxlen: Maximum number of entries to keep.
            minid: Remove entries older than this ID.

        Returns:
            Number of entries removed.
        """
        if not self._redis:
            return 0

        full_stream = self._full_stream_name(stream)

        try:
            if maxlen is not None:
                result = await self._redis.xtrim(full_stream, maxlen=maxlen)
                return int(result)
            elif minid is not None:
                result = await self._redis.xtrim(full_stream, minid=minid)
                return int(result)
            return 0
        except Exception as e:
            logger.error("Error trimming stream: %s", e)
            return 0

    async def get_pending_count(self, stream: str) -> int:
        """Get count of pending messages in a stream."""
        if not self._redis:
            return 0

        full_stream = self._full_stream_name(stream)

        try:
            info = await self._redis.xpending(full_stream, self.config.consumer_group)
            return info.get("pending", 0) if isinstance(info, dict) else 0
        except Exception:
            return 0
