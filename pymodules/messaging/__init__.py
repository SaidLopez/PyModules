"""
PyModules Messaging Package.

Provides distributed message broker integration for inter-service communication.
Supports Redis Streams out of the box, with protocol definitions for Kafka and RabbitMQ.

Example:
    from pymodules.messaging import RedisBroker, EventConsumer, Message

    # Configure broker
    broker = RedisBroker(url="redis://localhost:6379")

    # Publish a message
    await broker.publish("events.user.created", Message(
        data={"user_id": 123, "name": "John"},
        headers={"source": "auth-service"}
    ))

    # Consume messages
    consumer = EventConsumer(broker, host)
    await consumer.start()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Lazy imports for optional dependencies
__all__ = [
    # Core types
    "Message",
    "MessageBroker",
    "MessageBrokerConfig",
    # Redis implementation
    "RedisBroker",
    "RedisBrokerConfig",
    # Consumer
    "EventConsumer",
    "EventConsumerConfig",
    # Persistent DLQ
    "PersistentDeadLetterQueue",
    # Protocols
    "KafkaBrokerProtocol",
    "RabbitMQBrokerProtocol",
    # Exceptions
    "MessagingError",
    "BrokerConnectionError",
    "PublishError",
    "ConsumeError",
]


def __getattr__(name: str) -> object:
    """Lazy import messaging components."""
    if name in (
        "Message",
        "MessageBroker",
        "MessageBrokerConfig",
        "MessagingError",
        "BrokerConnectionError",
        "PublishError",
        "ConsumeError",
    ):
        from .broker import (
            BrokerConnectionError,
            ConsumeError,
            Message,
            MessageBroker,
            MessageBrokerConfig,
            MessagingError,
            PublishError,
        )

        return locals()[name]

    if name in ("RedisBroker", "RedisBrokerConfig"):
        from .redis_broker import RedisBroker, RedisBrokerConfig

        return locals()[name]

    if name in ("EventConsumer", "EventConsumerConfig"):
        from .consumer import EventConsumer, EventConsumerConfig

        return locals()[name]

    if name == "PersistentDeadLetterQueue":
        from .persistent_dlq import PersistentDeadLetterQueue

        return PersistentDeadLetterQueue

    if name in ("KafkaBrokerProtocol", "RabbitMQBrokerProtocol"):
        from .protocols import KafkaBrokerProtocol, RabbitMQBrokerProtocol

        return locals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from .broker import (
        BrokerConnectionError,
        ConsumeError,
        Message,
        MessageBroker,
        MessageBrokerConfig,
        MessagingError,
        PublishError,
    )
    from .consumer import EventConsumer, EventConsumerConfig
    from .persistent_dlq import PersistentDeadLetterQueue
    from .protocols import KafkaBrokerProtocol, RabbitMQBrokerProtocol
    from .redis_broker import RedisBroker, RedisBrokerConfig
