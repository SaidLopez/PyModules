"""
Protocol definitions for additional message brokers.

These protocols define the expected interface for Kafka and RabbitMQ adapters.
Implementations are not included to keep dependencies minimal, but the protocols
provide a blueprint for custom implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .broker import Message


# =============================================================================
# Kafka Protocol
# =============================================================================


@runtime_checkable
class KafkaBrokerProtocol(Protocol):
    """
    Protocol for Kafka message broker implementations.

    Example implementation using aiokafka:

        from aiokafka import AIOKafkaProducer, AIOKafkaConsumer

        class KafkaBroker(MessageBroker):
            def __init__(self, bootstrap_servers: str, config: MessageBrokerConfig = None):
                super().__init__(config)
                self.bootstrap_servers = bootstrap_servers
                self._producer: AIOKafkaProducer | None = None
                self._consumer: AIOKafkaConsumer | None = None

            async def connect(self) -> None:
                self._producer = AIOKafkaProducer(
                    bootstrap_servers=self.bootstrap_servers,
                    value_serializer=lambda v: json.dumps(v).encode()
                )
                await self._producer.start()
                self._connected = True

            async def publish(self, topic: str, message: Message) -> str:
                result = await self._producer.send_and_wait(
                    self._full_stream_name(topic),
                    message.to_dict()
                )
                return f"{result.partition}-{result.offset}"

            # ... implement other methods
    """

    bootstrap_servers: str
    client_id: str
    security_protocol: str

    async def connect(self) -> None:
        """Connect to Kafka cluster."""
        ...

    async def disconnect(self) -> None:
        """Disconnect from Kafka."""
        ...

    async def publish(self, topic: str, message: Message, key: str | None = None) -> str:
        """Publish message to Kafka topic."""
        ...

    async def subscribe(self, topics: list[str]) -> None:
        """Subscribe to Kafka topics."""
        ...

    async def consume(self) -> AsyncIterator[Message]:
        """Consume messages from subscribed topics."""
        ...

    async def commit(self) -> None:
        """Commit consumer offsets."""
        ...

    async def seek_to_beginning(self, topic: str, partition: int = 0) -> None:
        """Seek to beginning of topic partition."""
        ...

    async def seek_to_end(self, topic: str, partition: int = 0) -> None:
        """Seek to end of topic partition."""
        ...

    def get_partition_count(self, topic: str) -> int:
        """Get number of partitions for a topic."""
        ...


# =============================================================================
# RabbitMQ Protocol
# =============================================================================


@runtime_checkable
class RabbitMQBrokerProtocol(Protocol):
    """
    Protocol for RabbitMQ message broker implementations.

    Example implementation using aio-pika:

        import aio_pika

        class RabbitMQBroker(MessageBroker):
            def __init__(self, url: str, config: MessageBrokerConfig = None):
                super().__init__(config)
                self.url = url
                self._connection: aio_pika.Connection | None = None
                self._channel: aio_pika.Channel | None = None

            async def connect(self) -> None:
                self._connection = await aio_pika.connect_robust(self.url)
                self._channel = await self._connection.channel()
                await self._channel.set_qos(prefetch_count=self.config.batch_size)
                self._connected = True

            async def publish(self, exchange: str, message: Message,
                            routing_key: str = "") -> str:
                exchange_obj = await self._channel.get_exchange(exchange)
                msg = aio_pika.Message(
                    body=message.to_json().encode(),
                    message_id=message.id,
                    headers=message.headers
                )
                await exchange_obj.publish(msg, routing_key=routing_key)
                return message.id

            # ... implement other methods
    """

    url: str
    prefetch_count: int
    connection_name: str

    async def connect(self) -> None:
        """Connect to RabbitMQ."""
        ...

    async def disconnect(self) -> None:
        """Disconnect from RabbitMQ."""
        ...

    async def declare_exchange(
        self,
        name: str,
        exchange_type: str = "topic",
        durable: bool = True,
    ) -> None:
        """Declare an exchange."""
        ...

    async def declare_queue(
        self,
        name: str,
        durable: bool = True,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Declare a queue."""
        ...

    async def bind_queue(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "#",
    ) -> None:
        """Bind a queue to an exchange."""
        ...

    async def publish(
        self,
        exchange: str,
        message: Message,
        routing_key: str = "",
    ) -> str:
        """Publish message to exchange with routing key."""
        ...

    async def subscribe(self, queues: list[str]) -> None:
        """Subscribe to queues."""
        ...

    async def consume(self) -> AsyncIterator[Message]:
        """Consume messages from subscribed queues."""
        ...

    async def ack(self, message: Message) -> None:
        """Acknowledge message."""
        ...

    async def nack(self, message: Message, requeue: bool = True) -> None:
        """Negative acknowledge message."""
        ...

    async def reject(self, message: Message) -> None:
        """Reject message (don't requeue)."""
        ...


# =============================================================================
# Generic In-Memory Broker (for testing)
# =============================================================================


class InMemoryBrokerProtocol(Protocol):
    """
    Protocol for in-memory broker (useful for testing).

    Example implementation:

        class InMemoryBroker(MessageBroker):
            def __init__(self, config: MessageBrokerConfig = None):
                super().__init__(config)
                self._streams: dict[str, list[Message]] = {}
                self._subscriptions: set[str] = set()

            async def connect(self) -> None:
                self._connected = True

            async def publish(self, stream: str, message: Message) -> str:
                full_stream = self._full_stream_name(stream)
                if full_stream not in self._streams:
                    self._streams[full_stream] = []
                self._streams[full_stream].append(message)
                return message.id

            async def consume(self) -> AsyncIterator[Message]:
                for stream in self._subscriptions:
                    while self._streams.get(stream):
                        yield self._streams[stream].pop(0)
    """

    async def connect(self) -> None:
        """Connect (no-op for in-memory)."""
        ...

    async def disconnect(self) -> None:
        """Disconnect (clear state)."""
        ...

    async def publish(self, stream: str, message: Message) -> str:
        """Store message in memory."""
        ...

    async def subscribe(self, streams: list[str]) -> None:
        """Subscribe to streams."""
        ...

    async def consume(self) -> AsyncIterator[Message]:
        """Consume from in-memory streams."""
        ...

    def clear(self) -> None:
        """Clear all messages and subscriptions."""
        ...

    def get_messages(self, stream: str) -> list[Message]:
        """Get all messages in a stream (for testing)."""
        ...
