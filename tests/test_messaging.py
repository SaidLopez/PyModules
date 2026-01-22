"""
Tests for messaging package: broker, consumer, persistent DLQ.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module
from pymodules.messaging.broker import (
    BrokerConnectionError,
    ConsumeError,
    Message,
    MessageBroker,
    MessageBrokerConfig,
    PublishError,
)
from pymodules.messaging.consumer import (
    EventConsumer,
    EventConsumerConfig,
    ExternalEvent,
    ExternalEventInput,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@dataclass
class MsgTestInput(EventInput):
    value: str = ""


@dataclass
class MsgTestOutput(EventOutput):
    result: str = ""


class MsgTestEvent(Event[MsgTestInput, MsgTestOutput]):
    name = "test.messaging"


@module(name="ConsumerModule")
class ConsumerModule(Module):
    """Module that handles external events for testing."""

    def __init__(self) -> None:
        super().__init__()
        self.handled_events: list[Event] = []

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, ExternalEvent)

    def handle(self, event: Event) -> None:
        self.handled_events.append(event)
        event.handled = True


class MockBroker(MessageBroker):
    """In-memory mock broker for testing."""

    def __init__(self, config: MessageBrokerConfig | None = None) -> None:
        super().__init__(config)
        self.messages: dict[str, list[Message]] = {}
        self.subscriptions: list[str] = []
        self.acked: list[str] = []
        self.nacked: list[str] = []

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def publish(self, stream: str, message: Message) -> str:
        full_stream = self._full_stream_name(stream)
        message.stream = full_stream
        if full_stream not in self.messages:
            self.messages[full_stream] = []
        self.messages[full_stream].append(message)
        return message.id

    async def subscribe(self, streams: list[str]) -> None:
        for stream in streams:
            full_stream = self._full_stream_name(stream)
            if full_stream not in self.subscriptions:
                self.subscriptions.append(full_stream)

    async def consume(self):
        for stream in self.subscriptions:
            if stream in self.messages:
                while self.messages[stream]:
                    yield self.messages[stream].pop(0)

    async def ack(self, message: Message) -> None:
        self.acked.append(message.id)

    async def nack(self, message: Message, requeue: bool = True) -> None:
        self.nacked.append(message.id)

    async def health_check(self) -> bool:
        return self._connected

    async def stream_info(self, stream: str) -> dict:
        full_stream = self._full_stream_name(stream)
        return {
            "length": len(self.messages.get(full_stream, [])),
            "groups": 1,
        }


# =============================================================================
# Message Tests
# =============================================================================


class TestMessage:
    """Tests for Message class."""

    def test_message_creation(self) -> None:
        """Message can be created with data."""
        msg = Message(
            data={"user_id": 123},
            headers={"trace_id": "abc"},
            event_name="user.created",
        )

        assert msg.data == {"user_id": 123}
        assert msg.headers == {"trace_id": "abc"}
        assert msg.event_name == "user.created"
        assert msg.id  # Auto-generated
        assert msg.timestamp > 0

    def test_message_serialization(self) -> None:
        """Message can be serialized to dict and JSON."""
        msg = Message(
            id="test-id",
            data={"key": "value"},
            headers={"header": "value"},
            timestamp=1234567890.0,
            event_name="test.event",
        )

        # To dict
        d = msg.to_dict()
        assert d["id"] == "test-id"
        assert d["data"] == {"key": "value"}
        assert d["headers"] == {"header": "value"}
        assert d["timestamp"] == 1234567890.0
        assert d["event_name"] == "test.event"

        # To JSON
        j = msg.to_json()
        assert isinstance(j, str)
        parsed = json.loads(j)
        assert parsed["id"] == "test-id"

    def test_message_deserialization(self) -> None:
        """Message can be deserialized from dict and JSON."""
        data = {
            "id": "test-id",
            "data": {"key": "value"},
            "headers": {"header": "value"},
            "timestamp": 1234567890.0,
            "event_name": "test.event",
        }

        # From dict
        msg = Message.from_dict(data, stream="test-stream")
        assert msg.id == "test-id"
        assert msg.data == {"key": "value"}
        assert msg.stream == "test-stream"

        # From JSON
        json_str = json.dumps(data)
        msg2 = Message.from_json(json_str)
        assert msg2.id == "test-id"


# =============================================================================
# MessageBrokerConfig Tests
# =============================================================================


class TestMessageBrokerConfig:
    """Tests for MessageBrokerConfig."""

    def test_default_config(self) -> None:
        """Default configuration has sensible values."""
        config = MessageBrokerConfig()

        assert config.stream_prefix == "pymodules:"
        assert config.consumer_group == "pymodules-workers"
        assert config.consumer_name.startswith("worker-")
        assert config.max_retries == 3
        assert config.batch_size == 10

    def test_config_from_env(self, monkeypatch) -> None:
        """Configuration can be loaded from environment."""
        monkeypatch.setenv("PYMODULES_BROKER_STREAM_PREFIX", "myapp:")
        monkeypatch.setenv("PYMODULES_BROKER_CONSUMER_GROUP", "my-workers")
        monkeypatch.setenv("PYMODULES_BROKER_MAX_RETRIES", "5")
        monkeypatch.setenv("PYMODULES_BROKER_BATCH_SIZE", "20")

        config = MessageBrokerConfig.from_env()

        assert config.stream_prefix == "myapp:"
        assert config.consumer_group == "my-workers"
        assert config.max_retries == 5
        assert config.batch_size == 20


# =============================================================================
# Mock Broker Tests
# =============================================================================


class TestMockBroker:
    """Tests for mock broker (validates broker interface)."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self) -> None:
        """Broker can connect and disconnect."""
        broker = MockBroker()

        assert not broker.connected
        await broker.connect()
        assert broker.connected
        await broker.disconnect()
        assert not broker.connected

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """Broker works as async context manager."""
        async with MockBroker() as broker:
            assert broker.connected
        assert not broker.connected

    @pytest.mark.asyncio
    async def test_publish(self) -> None:
        """Broker can publish messages."""
        async with MockBroker() as broker:
            msg = Message(data={"test": "data"}, event_name="test.event")
            msg_id = await broker.publish("events", msg)

            assert msg_id == msg.id
            assert "pymodules:events" in broker.messages
            assert len(broker.messages["pymodules:events"]) == 1

    @pytest.mark.asyncio
    async def test_subscribe_consume(self) -> None:
        """Broker can subscribe and consume messages."""
        async with MockBroker() as broker:
            # Publish first
            msg = Message(data={"test": "data"})
            await broker.publish("events", msg)

            # Subscribe
            await broker.subscribe(["events"])
            assert "pymodules:events" in broker.subscriptions

            # Consume
            consumed = []
            async for message in broker.consume():
                consumed.append(message)

            assert len(consumed) == 1
            assert consumed[0].data == {"test": "data"}

    @pytest.mark.asyncio
    async def test_ack_nack(self) -> None:
        """Broker can ack and nack messages."""
        async with MockBroker() as broker:
            msg = Message(data={"test": "data"})

            await broker.ack(msg)
            assert msg.id in broker.acked

            await broker.nack(msg)
            assert msg.id in broker.nacked

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        """Broker health check works."""
        broker = MockBroker()

        assert not await broker.health_check()
        await broker.connect()
        assert await broker.health_check()

    @pytest.mark.asyncio
    async def test_stream_info(self) -> None:
        """Broker returns stream info."""
        async with MockBroker() as broker:
            # Publish some messages
            for i in range(5):
                await broker.publish("events", Message(data={"i": i}))

            info = await broker.stream_info("events")
            assert info["length"] == 5


# =============================================================================
# EventConsumer Tests
# =============================================================================


class TestEventConsumer:
    """Tests for EventConsumer."""

    @pytest.mark.asyncio
    async def test_consumer_creation(self) -> None:
        """Consumer can be created with broker and host."""
        broker = MockBroker()
        host = ModuleHost()

        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["events"]),
        )

        assert consumer.broker is broker
        assert consumer.host is host
        assert not consumer.running

    @pytest.mark.asyncio
    async def test_consumer_handles_messages(self) -> None:
        """Consumer dispatches messages to host."""
        broker = MockBroker()
        host = ModuleHost()
        handler = ConsumerModule()
        host.register(handler)

        # Connect and subscribe first
        await broker.connect()
        await broker.subscribe(["events"])

        # Publish a message
        await broker.publish(
            "events",
            Message(
                data={"user": "test"},
                event_name="user.created",
            ),
        )

        # Create and run consumer
        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["events"]),
        )

        count = await consumer.process_once(timeout=1.0)

        assert count == 1
        assert len(handler.handled_events) == 1
        assert handler.handled_events[0].name == "user.created"

    @pytest.mark.asyncio
    async def test_consumer_auto_acks(self) -> None:
        """Consumer auto-acks handled messages."""
        broker = MockBroker()
        host = ModuleHost()
        handler = ConsumerModule()
        host.register(handler)

        await broker.connect()
        await broker.subscribe(["events"])
        msg = Message(data={"test": "data"})
        await broker.publish("events", msg)

        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["events"], auto_ack=True),
        )

        await consumer.process_once(timeout=1.0)

        assert msg.id in broker.acked

    @pytest.mark.asyncio
    async def test_consumer_config_from_env(self, monkeypatch) -> None:
        """Consumer config can be loaded from environment."""
        monkeypatch.setenv("PYMODULES_CONSUMER_STREAMS", "stream1,stream2,stream3")
        monkeypatch.setenv("PYMODULES_CONSUMER_AUTO_ACK", "false")
        monkeypatch.setenv("PYMODULES_CONSUMER_CONCURRENCY", "20")

        config = EventConsumerConfig.from_env()

        assert config.streams == ["stream1", "stream2", "stream3"]
        assert config.auto_ack is False
        assert config.concurrency == 20

    @pytest.mark.asyncio
    async def test_consumer_stream_mapping(self) -> None:
        """Consumer can map streams to event names."""
        broker = MockBroker()
        host = ModuleHost()
        handler = ConsumerModule()
        host.register(handler)

        await broker.connect()
        await broker.subscribe(["orders"])
        # Message without event_name
        await broker.publish("orders", Message(data={"order": 123}))

        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["orders"]),
        )

        # Map stream to event name
        consumer.add_stream_mapping("pymodules:orders", "order.created")

        await consumer.process_once(timeout=1.0)

        assert len(handler.handled_events) == 1
        assert handler.handled_events[0].name == "order.created"


# =============================================================================
# ExternalEvent Tests
# =============================================================================


class TestExternalEvent:
    """Tests for ExternalEvent and ExternalEventInput."""

    def test_external_event_input(self) -> None:
        """ExternalEventInput contains message data."""
        input_data = ExternalEventInput(
            data={"user_id": 123},
            headers={"trace_id": "abc"},
            source_stream="users",
            message_id="msg-123",
        )

        assert input_data.data == {"user_id": 123}
        assert input_data.headers == {"trace_id": "abc"}
        assert input_data.source_stream == "users"
        assert input_data.message_id == "msg-123"

    def test_external_event_creation(self) -> None:
        """ExternalEvent can be created with input."""
        event = ExternalEvent(
            name="user.created",
            input=ExternalEventInput(data={"user": "test"}),
        )

        assert event.name == "user.created"
        assert event.input.data == {"user": "test"}


# =============================================================================
# Integration Tests
# =============================================================================


class TestMessagingIntegration:
    """Integration tests for messaging components."""

    @pytest.mark.asyncio
    async def test_end_to_end_message_flow(self) -> None:
        """Messages flow from publish to consume."""
        broker = MockBroker()
        host = ModuleHost()
        handler = ConsumerModule()
        host.register(handler)

        # Setup consumer
        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["notifications"]),
        )

        async with broker:
            await broker.subscribe(["notifications"])
            # Publish messages
            for i in range(3):
                await broker.publish(
                    "notifications",
                    Message(
                        data={"notification_id": i},
                        event_name="notification.sent",
                    ),
                )

            # Consume all - need to process all in one go since mock consumer yields all
            await consumer.process_once(timeout=2.0)

        # Verify all handled
        assert len(handler.handled_events) == 3
        for event in handler.handled_events:
            assert event.name == "notification.sent"

    @pytest.mark.asyncio
    async def test_trace_context_propagation(self) -> None:
        """Trace context is propagated through messages."""
        broker = MockBroker()
        host = ModuleHost()
        handler = ConsumerModule()
        host.register(handler)

        await broker.connect()
        await broker.subscribe(["events"])

        # Publish with trace headers
        await broker.publish(
            "events",
            Message(
                data={"test": "data"},
                headers={
                    "trace_id": "trace-123",
                    "span_id": "span-456",
                    "correlation_id": "corr-789",
                },
            ),
        )

        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["events"]),
        )

        await consumer.process_once(timeout=1.0)

        # Verify trace context in event meta
        event = handler.handled_events[0]
        assert event.meta.get("trace_id") == "trace-123"
        assert event.meta.get("span_id") == "span-456"
        assert event.meta.get("correlation_id") == "corr-789"
