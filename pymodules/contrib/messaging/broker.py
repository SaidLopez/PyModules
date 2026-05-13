"""
Abstract message broker interface for PyModules.

Defines the base protocol for message brokers (Redis, Kafka, RabbitMQ, etc.).
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


# =============================================================================
# Exceptions
# =============================================================================


class MessagingError(Exception):
    """Base exception for messaging errors."""

    pass


class BrokerConnectionError(MessagingError):
    """Raised when broker connection fails."""

    pass


class PublishError(MessagingError):
    """Raised when message publishing fails."""

    pass


class ConsumeError(MessagingError):
    """Raised when message consumption fails."""

    pass


# =============================================================================
# Message Types
# =============================================================================


@dataclass
class Message:
    """
    A message to be sent through a message broker.

    Attributes:
        data: The message payload (dict, will be JSON serialized).
        headers: Optional message headers/metadata.
        id: Unique message identifier (auto-generated if not provided).
        timestamp: Message creation timestamp.
        stream: Target stream/topic (set by broker on publish).
        event_name: PyModules event name for routing.

    Example:
        msg = Message(
            data={"user_id": 123, "action": "login"},
            headers={"source": "auth-service", "trace_id": "abc123"},
            event_name="user.logged_in"
        )
    """

    data: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    stream: str = ""
    event_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize message to dictionary."""
        return {
            "id": self.id,
            "data": self.data,
            "headers": self.headers,
            "timestamp": self.timestamp,
            "event_name": self.event_name,
        }

    def to_json(self) -> str:
        """Serialize message to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any], stream: str = "") -> Message:
        """Deserialize message from dictionary."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            data=data.get("data", {}),
            headers=data.get("headers", {}),
            timestamp=data.get("timestamp", time.time()),
            event_name=data.get("event_name", ""),
            stream=stream,
        )

    @classmethod
    def from_json(cls, json_str: str, stream: str = "") -> Message:
        """Deserialize message from JSON string."""
        return cls.from_dict(json.loads(json_str), stream=stream)


# =============================================================================
# Broker Configuration
# =============================================================================


@dataclass
class MessageBrokerConfig:
    """
    Base configuration for message brokers.

    Attributes:
        stream_prefix: Prefix for all stream/topic names.
        consumer_group: Consumer group name for this application.
        consumer_name: Unique consumer name within the group.
        max_retries: Maximum delivery attempts before sending to DLQ.
        ack_timeout: Seconds to wait for message acknowledgment.
        batch_size: Number of messages to fetch per read.
        block_timeout: Milliseconds to block waiting for new messages.

    Environment Variables:
        PYMODULES_BROKER_STREAM_PREFIX
        PYMODULES_BROKER_CONSUMER_GROUP
        PYMODULES_BROKER_CONSUMER_NAME
        PYMODULES_BROKER_MAX_RETRIES
        PYMODULES_BROKER_ACK_TIMEOUT
        PYMODULES_BROKER_BATCH_SIZE
        PYMODULES_BROKER_BLOCK_TIMEOUT
    """

    stream_prefix: str = "pymodules:"
    consumer_group: str = "pymodules-workers"
    consumer_name: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    max_retries: int = 3
    ack_timeout: float = 30.0
    batch_size: int = 10
    block_timeout: int = 5000  # milliseconds

    @classmethod
    def from_env(cls) -> MessageBrokerConfig:
        """Load configuration from environment variables."""
        import os

        return cls(
            stream_prefix=os.getenv("PYMODULES_BROKER_STREAM_PREFIX", "pymodules:"),
            consumer_group=os.getenv("PYMODULES_BROKER_CONSUMER_GROUP", "pymodules-workers"),
            consumer_name=os.getenv(
                "PYMODULES_BROKER_CONSUMER_NAME", f"worker-{uuid.uuid4().hex[:8]}"
            ),
            max_retries=int(os.getenv("PYMODULES_BROKER_MAX_RETRIES", "3")),
            ack_timeout=float(os.getenv("PYMODULES_BROKER_ACK_TIMEOUT", "30.0")),
            batch_size=int(os.getenv("PYMODULES_BROKER_BATCH_SIZE", "10")),
            block_timeout=int(os.getenv("PYMODULES_BROKER_BLOCK_TIMEOUT", "5000")),
        )


# =============================================================================
# Abstract Broker Interface
# =============================================================================


class MessageBroker(ABC):
    """
    Abstract base class for message brokers.

    Implementations must provide async methods for:
    - connect/disconnect lifecycle
    - publish messages to streams/topics
    - consume messages from streams/topics
    - acknowledge processed messages

    Example Implementation:
        class MyBroker(MessageBroker):
            async def connect(self) -> None:
                self._client = await create_client()

            async def disconnect(self) -> None:
                await self._client.close()

            async def publish(self, stream: str, message: Message) -> str:
                return await self._client.send(stream, message.to_json())

            # ... other methods
    """

    def __init__(self, config: MessageBrokerConfig | None = None):
        """
        Initialize the broker.

        Args:
            config: Broker configuration. Uses defaults if not provided.
        """
        self.config = config or MessageBrokerConfig()
        self._connected = False

    @property
    def connected(self) -> bool:
        """Check if broker is connected."""
        return self._connected

    def _full_stream_name(self, stream: str) -> str:
        """Get full stream name with prefix."""
        if stream.startswith(self.config.stream_prefix):
            return stream
        return f"{self.config.stream_prefix}{stream}"

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """
        Establish connection to the message broker.

        Raises:
            BrokerConnectionError: If connection fails.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close connection to the message broker.

        Should be safe to call multiple times.
        """
        pass

    async def __aenter__(self) -> MessageBroker:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type | None, exc_val: Exception | None, exc_tb: object
    ) -> None:
        """Async context manager exit."""
        await self.disconnect()

    # -------------------------------------------------------------------------
    # Publishing
    # -------------------------------------------------------------------------

    @abstractmethod
    async def publish(self, stream: str, message: Message) -> str:
        """
        Publish a message to a stream/topic.

        Args:
            stream: Target stream name (prefix will be added).
            message: Message to publish.

        Returns:
            Message ID assigned by the broker.

        Raises:
            PublishError: If publishing fails.
        """
        pass

    async def publish_batch(self, stream: str, messages: list[Message]) -> list[str]:
        """
        Publish multiple messages to a stream.

        Default implementation calls publish() for each message.
        Subclasses may override for more efficient batch operations.

        Args:
            stream: Target stream name.
            messages: List of messages to publish.

        Returns:
            List of message IDs.
        """
        return [await self.publish(stream, msg) for msg in messages]

    # -------------------------------------------------------------------------
    # Consuming
    # -------------------------------------------------------------------------

    @abstractmethod
    async def subscribe(self, streams: list[str]) -> None:
        """
        Subscribe to one or more streams.

        Creates consumer groups if they don't exist.

        Args:
            streams: List of stream names to subscribe to.
        """
        pass

    @abstractmethod
    async def consume(self) -> AsyncIterator[Message]:
        """
        Consume messages from subscribed streams.

        Yields messages one at a time. Must call ack() after processing.

        Yields:
            Messages from subscribed streams.

        Raises:
            ConsumeError: If consumption fails.
        """
        # This is an abstract generator - subclass must implement
        yield  # type: ignore[misc]  # pragma: no cover

    @abstractmethod
    async def ack(self, message: Message) -> None:
        """
        Acknowledge a message as processed.

        Args:
            message: The message to acknowledge.
        """
        pass

    @abstractmethod
    async def nack(self, message: Message, requeue: bool = True) -> None:
        """
        Negatively acknowledge a message (processing failed).

        Args:
            message: The message that failed.
            requeue: If True, message will be redelivered. If False, goes to DLQ.
        """
        pass

    # -------------------------------------------------------------------------
    # Health & Info
    # -------------------------------------------------------------------------

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check broker health.

        Returns:
            True if broker is healthy and connected.
        """
        pass

    @abstractmethod
    async def stream_info(self, stream: str) -> dict[str, Any]:
        """
        Get information about a stream.

        Args:
            stream: Stream name.

        Returns:
            Dictionary with stream metadata (length, groups, etc.).
        """
        pass

    # -------------------------------------------------------------------------
    # Optional: Callbacks
    # -------------------------------------------------------------------------

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Register callback for connection events."""
        self._on_connect = callback

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register callback for disconnection events."""
        self._on_disconnect = callback

    def on_error(self, callback: Callable[[Exception], None]) -> None:
        """Register callback for error events."""
        self._on_error = callback
