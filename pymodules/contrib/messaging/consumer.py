"""
EventConsumer - Bridges message broker to ModuleHost.

Consumes Events from a broker (the cross-process broadcast notification) and
dispatches them through the host as commands, wrapped in ``ExternalEvent``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...interfaces import Command, CommandRequest, CommandResponse
from ...logging import get_logger
from .broker import ConsumeError, Message, MessageBroker

if TYPE_CHECKING:
    from ..host import ModuleHost

logger = get_logger("messaging.consumer")


# =============================================================================
# Command Input/Output for External Broker Events
# =============================================================================


@dataclass
class ExternalEventInput(CommandRequest):
    """Request payload for an Event received from an external message broker."""

    data: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    source_stream: str = ""
    message_id: str = ""


@dataclass
class ExternalEventOutput(CommandResponse):
    """Response payload for an externally-triggered Event."""

    result: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None


class ExternalEvent(Command[ExternalEventInput, ExternalEventOutput]):
    """In-process wrapper for an Event received from an external broker."""

    pass


# =============================================================================
# Consumer Configuration
# =============================================================================


@dataclass
class EventConsumerConfig:
    """
    Configuration for EventConsumer.

    Attributes:
        streams: List of streams to consume from.
        auto_ack: Automatically acknowledge messages after successful handling.
        create_events: If True, create ExternalEvent for messages without event_name.
        event_mapping: Map stream names to event names.
        concurrency: Number of concurrent message handlers.
        shutdown_timeout: Seconds to wait for graceful shutdown.

    Environment Variables:
        PYMODULES_CONSUMER_STREAMS
        PYMODULES_CONSUMER_AUTO_ACK
        PYMODULES_CONSUMER_CONCURRENCY
        PYMODULES_CONSUMER_SHUTDOWN_TIMEOUT
    """

    streams: list[str] = field(default_factory=list)
    auto_ack: bool = True
    create_events: bool = True
    event_mapping: dict[str, str] = field(default_factory=dict)
    concurrency: int = 10
    shutdown_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> EventConsumerConfig:
        """Load configuration from environment variables."""
        import os

        streams_str = os.getenv("PYMODULES_CONSUMER_STREAMS", "")
        streams = [s.strip() for s in streams_str.split(",") if s.strip()]

        return cls(
            streams=streams,
            auto_ack=os.getenv("PYMODULES_CONSUMER_AUTO_ACK", "true").lower() == "true",
            concurrency=int(os.getenv("PYMODULES_CONSUMER_CONCURRENCY", "10")),
            shutdown_timeout=float(os.getenv("PYMODULES_CONSUMER_SHUTDOWN_TIMEOUT", "30.0")),
        )


# =============================================================================
# Event Consumer
# =============================================================================


class EventConsumer:
    """
    Consumes Events from a broker and dispatches them as commands to ModuleHost.

    Bridges external message queues with the PyModules dispatch system, allowing
    modules to handle broker Events from distributed sources.

    Example:
        broker = RedisBroker(config)
        host = ModuleHost()
        host.register(MyHandler())

        consumer = EventConsumer(
            broker=broker,
            host=host,
            config=EventConsumerConfig(streams=["events.users", "events.orders"])
        )

        # Start consuming
        await consumer.start()

        # Later, stop gracefully
        await consumer.stop()
    """

    def __init__(
        self,
        broker: MessageBroker,
        host: ModuleHost,
        config: EventConsumerConfig | None = None,
    ):
        """
        Initialize the consumer.

        Args:
            broker: Message broker to consume from.
            host: ModuleHost to dispatch commands to.
            config: Consumer configuration.
        """
        self.broker = broker
        self.host = host
        self.config = config or EventConsumerConfig()

        self._running = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._consumer_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Check if consumer is running."""
        return self._running

    async def start(self) -> None:
        """
        Start consuming messages.

        Connects to the broker, subscribes to configured streams,
        and begins processing messages.
        """
        if self._running:
            logger.warning("Consumer already running")
            return

        if not self.config.streams:
            raise ConsumeError("No streams configured for consumption")

        # Connect broker if not connected
        if not self.broker.connected:
            await self.broker.connect()

        # Subscribe to streams
        await self.broker.subscribe(self.config.streams)

        # Initialize concurrency control
        self._semaphore = asyncio.Semaphore(self.config.concurrency)
        self._running = True

        # Start consumer loop
        self._consumer_task = asyncio.create_task(self._consume_loop())

        logger.info(
            "EventConsumer started, consuming from streams: %s",
            ", ".join(self.config.streams),
        )

    async def stop(self) -> None:
        """
        Stop consuming messages gracefully.

        Waits for in-flight messages to complete up to shutdown_timeout.
        """
        if not self._running:
            return

        logger.info("Stopping EventConsumer...")
        self._running = False

        # Cancel consumer task
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await asyncio.wait_for(
                    self._consumer_task,
                    timeout=self.config.shutdown_timeout,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Wait for pending handlers
        if self._tasks:
            logger.info("Waiting for %d pending handlers...", len(self._tasks))
            done, pending = await asyncio.wait(
                self._tasks,
                timeout=self.config.shutdown_timeout,
            )

            # Cancel any remaining tasks
            for task in pending:
                task.cancel()

        self._tasks.clear()
        logger.info("EventConsumer stopped")

    async def _consume_loop(self) -> None:
        """Main consumption loop."""
        try:
            async for message in self.broker.consume():
                if not self._running:
                    break

                # Spawn handler with concurrency limit
                if self._semaphore:
                    await self._semaphore.acquire()

                task = asyncio.create_task(self._handle_message(message))
                self._tasks.add(task)
                task.add_done_callback(self._task_done)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Consumer loop error: %s", e)
            raise

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Callback when a handler task completes."""
        self._tasks.discard(task)
        if self._semaphore:
            self._semaphore.release()

    async def _handle_message(self, message: Message) -> None:
        """Handle a single broker message by dispatching it as a command."""
        try:
            # Build command wrapper from broker message
            command = self._build_command(message)

            if command is None:
                logger.warning(
                    "Could not build command from message %s (no event_name and create_events=False)",
                    message.id,
                )
                if self.config.auto_ack:
                    await self.broker.ack(message)
                return

            # Dispatch through host
            logger.debug("Dispatching %s from message %s", command.name, message.id)

            # Use async dispatch
            await self.host.dispatch_async(command)

            if command.handled:
                logger.debug("Command %s handled successfully", command.name)
                if self.config.auto_ack:
                    await self.broker.ack(message)
            else:
                logger.warning("Command %s was not handled by any module", command.name)
                if self.config.auto_ack:
                    # Still ack - no handler is not an error
                    await self.broker.ack(message)

        except Exception as e:
            logger.error("Error handling message %s: %s", message.id, e)
            await self.broker.nack(message, requeue=True)

    def _build_command(self, message: Message) -> Command[Any, Any] | None:
        """Build an in-process Command wrapping a broker Message."""
        # Determine the broker event name carried on the message
        event_name = message.event_name

        # Check stream-to-event mapping
        if not event_name and message.stream in self.config.event_mapping:
            event_name = self.config.event_mapping[message.stream]

        # Use stream name as event name
        if not event_name:
            # Extract event name from stream (remove prefix)
            stream = message.stream
            prefix = self.broker.config.stream_prefix
            if stream.startswith(prefix):
                event_name = stream[len(prefix) :]
            else:
                event_name = stream

        if not event_name and not self.config.create_events:
            return None

        # Build the in-process Command with broker-message data
        request = ExternalEventInput(
            data=message.data,
            headers=message.headers,
            source_stream=message.stream,
            message_id=message.id,
        )

        command = ExternalEvent(
            name=event_name,
            input=request,
        )

        # Copy trace context from headers
        if "trace_id" in message.headers:
            command.meta["trace_id"] = message.headers["trace_id"]
        if "span_id" in message.headers:
            command.meta["span_id"] = message.headers["span_id"]
        if "correlation_id" in message.headers:
            command.meta["correlation_id"] = message.headers["correlation_id"]

        return command

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    async def process_once(self, timeout: float = 5.0) -> int:
        """
        Process messages once (useful for testing).

        Args:
            timeout: Maximum time to wait for messages.

        Returns:
            Number of messages processed.
        """
        if not self.broker.connected:
            await self.broker.connect()
            await self.broker.subscribe(self.config.streams)

        count = 0
        start = asyncio.get_event_loop().time()

        async for message in self.broker.consume():
            await self._handle_message(message)
            count += 1

            # Check timeout
            if asyncio.get_event_loop().time() - start >= timeout:
                break

        return count

    def add_stream_mapping(self, stream: str, event_name: str) -> None:
        """Add a stream-to-event name mapping."""
        self.config.event_mapping[stream] = event_name

    @property
    def pending_count(self) -> int:
        """Get count of pending handler tasks."""
        return len(self._tasks)
