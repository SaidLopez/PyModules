"""
Tests for the in-process EventBus and the ``@subscribes`` Module decorator.
"""

import asyncio
from dataclasses import dataclass

import pytest

from pymodules import (
    Event,
    EventBus,
    Module,
    ModuleHost,
    handles,
    subscribes,
)
from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
)
from pymodules.module import SUBSCRIBES_ATTR


# ---------------------------------------------------------------------------
# Event fixtures
# ---------------------------------------------------------------------------


@dataclass
class UserCreated(Event):
    user_id: str = ""
    email: str = ""
    name: str = "user.created"


@dataclass
class OrderPlaced(Event):
    order_id: str = ""
    name: str = "order.placed"


@dataclass
class DerivedUserCreated(UserCreated):
    """Subclass to verify exact-type routing (not inheritance fan-out)."""

    extra: str = ""


# ---------------------------------------------------------------------------
# EventBus — subscribe / publish basics
# ---------------------------------------------------------------------------


class TestEventBusSubscribeAndPublish:
    """Core subscribe + publish behaviour."""

    def test_publish_with_no_subscribers_is_noop(self):
        bus = EventBus()
        # Must not raise.
        bus.publish(UserCreated(user_id="u-1"))

    def test_subscribe_and_publish_invokes_handler(self):
        bus = EventBus()
        received: list[UserCreated] = []

        def listener(event: UserCreated) -> None:
            received.append(event)

        bus.subscribe(UserCreated, listener)
        bus.publish(UserCreated(user_id="u-1"))

        assert len(received) == 1
        assert received[0].user_id == "u-1"

    def test_multi_subscriber_fanout(self):
        bus = EventBus()
        a: list[str] = []
        b: list[str] = []
        c: list[str] = []

        bus.subscribe(UserCreated, lambda e: a.append(e.user_id))
        bus.subscribe(UserCreated, lambda e: b.append(e.user_id))
        bus.subscribe(UserCreated, lambda e: c.append(e.user_id))

        bus.publish(UserCreated(user_id="u-1"))

        assert a == ["u-1"]
        assert b == ["u-1"]
        assert c == ["u-1"]

    def test_subscribers_invoked_in_registration_order(self):
        bus = EventBus()
        order: list[str] = []
        bus.subscribe(UserCreated, lambda _: order.append("first"))
        bus.subscribe(UserCreated, lambda _: order.append("second"))
        bus.subscribe(UserCreated, lambda _: order.append("third"))

        bus.publish(UserCreated(user_id="u-1"))

        assert order == ["first", "second", "third"]

    def test_subscribers_isolated_by_event_type(self):
        bus = EventBus()
        users: list[str] = []
        orders: list[str] = []
        bus.subscribe(UserCreated, lambda e: users.append(e.user_id))
        bus.subscribe(OrderPlaced, lambda e: orders.append(e.order_id))

        bus.publish(UserCreated(user_id="u-1"))
        bus.publish(OrderPlaced(order_id="o-9"))

        assert users == ["u-1"]
        assert orders == ["o-9"]


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    """One subscriber raising must not prevent others from receiving the event."""

    def test_raise_in_one_subscriber_does_not_block_others(self):
        bus = EventBus()
        good_called: list[str] = []

        def bad(event: UserCreated) -> None:
            raise RuntimeError("boom")

        def good(event: UserCreated) -> None:
            good_called.append(event.user_id)

        bus.subscribe(UserCreated, bad)
        bus.subscribe(UserCreated, good)

        # Publisher does not see the exception.
        bus.publish(UserCreated(user_id="u-1"))

        assert good_called == ["u-1"]

    def test_raise_in_late_subscriber_does_not_block_earlier(self):
        bus = EventBus()
        early: list[str] = []

        def good(event: UserCreated) -> None:
            early.append(event.user_id)

        def bad(event: UserCreated) -> None:
            raise RuntimeError("boom")

        bus.subscribe(UserCreated, good)
        bus.subscribe(UserCreated, bad)
        bus.publish(UserCreated(user_id="u-1"))

        assert early == ["u-1"]

    def test_async_subscriber_raise_isolated(self):
        bus = EventBus()
        good_called: list[str] = []

        async def bad(event: UserCreated) -> None:
            raise RuntimeError("async boom")

        def good(event: UserCreated) -> None:
            good_called.append(event.user_id)

        bus.subscribe(UserCreated, bad)
        bus.subscribe(UserCreated, good)

        async def run() -> None:
            await bus.publish_async(UserCreated(user_id="u-1"))

        asyncio.run(run())
        assert good_called == ["u-1"]


# ---------------------------------------------------------------------------
# Sync + async subscribers
# ---------------------------------------------------------------------------


class TestSyncAndAsyncSubscribers:
    def test_sync_subscriber_on_publish_async(self):
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(UserCreated, lambda e: seen.append(e.user_id))

        async def run() -> None:
            await bus.publish_async(UserCreated(user_id="u-1"))

        asyncio.run(run())
        assert seen == ["u-1"]

    def test_async_subscriber_on_publish_async(self):
        bus = EventBus()
        seen: list[str] = []

        async def listener(event: UserCreated) -> None:
            await asyncio.sleep(0)
            seen.append(event.user_id)

        bus.subscribe(UserCreated, listener)

        async def run() -> None:
            await bus.publish_async(UserCreated(user_id="u-1"))

        asyncio.run(run())
        assert seen == ["u-1"]

    def test_mixed_sync_and_async_on_publish_async(self):
        bus = EventBus()
        order: list[str] = []

        async def async_first(event: UserCreated) -> None:
            order.append("async-first")

        def sync_middle(event: UserCreated) -> None:
            order.append("sync-middle")

        async def async_last(event: UserCreated) -> None:
            order.append("async-last")

        bus.subscribe(UserCreated, async_first)
        bus.subscribe(UserCreated, sync_middle)
        bus.subscribe(UserCreated, async_last)

        async def run() -> None:
            await bus.publish_async(UserCreated(user_id="u-1"))

        asyncio.run(run())
        assert order == ["async-first", "sync-middle", "async-last"]

    def test_async_subscriber_bridged_from_sync_publish(self):
        """Sync publish() must still drive async subscribers to completion."""
        bus = EventBus()
        seen: list[str] = []

        async def listener(event: UserCreated) -> None:
            await asyncio.sleep(0)
            seen.append(event.user_id)

        bus.subscribe(UserCreated, listener)
        bus.publish(UserCreated(user_id="u-1"))
        assert seen == ["u-1"]


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        seen: list[str] = []

        def listener(event: UserCreated) -> None:
            seen.append(event.user_id)

        bus.subscribe(UserCreated, listener)
        bus.publish(UserCreated(user_id="u-1"))
        removed = bus.unsubscribe(UserCreated, listener)
        bus.publish(UserCreated(user_id="u-2"))

        assert removed is True
        assert seen == ["u-1"]

    def test_unsubscribe_unknown_returns_false(self):
        bus = EventBus()

        def listener(event: UserCreated) -> None:
            pass

        assert bus.unsubscribe(UserCreated, listener) is False

    def test_unsubscribe_one_of_two_keeps_other(self):
        bus = EventBus()
        a: list[str] = []
        b: list[str] = []

        def lis_a(event: UserCreated) -> None:
            a.append(event.user_id)

        def lis_b(event: UserCreated) -> None:
            b.append(event.user_id)

        bus.subscribe(UserCreated, lis_a)
        bus.subscribe(UserCreated, lis_b)

        bus.unsubscribe(UserCreated, lis_a)
        bus.publish(UserCreated(user_id="u-1"))

        assert a == []
        assert b == ["u-1"]

    def test_has_subscribers_and_count(self):
        bus = EventBus()
        assert bus.has_subscribers(UserCreated) is False
        assert bus.subscriber_count(UserCreated) == 0

        bus.subscribe(UserCreated, lambda _: None)
        bus.subscribe(UserCreated, lambda _: None)

        assert bus.has_subscribers(UserCreated) is True
        assert bus.subscriber_count(UserCreated) == 2

    def test_clear_removes_all(self):
        bus = EventBus()
        bus.subscribe(UserCreated, lambda _: None)
        bus.subscribe(OrderPlaced, lambda _: None)
        bus.clear()

        assert bus.has_subscribers(UserCreated) is False
        assert bus.has_subscribers(OrderPlaced) is False


# ---------------------------------------------------------------------------
# Exact-type routing (NOT inheritance fan-out)
# ---------------------------------------------------------------------------


class TestExactTypeRouting:
    """
    Subscribers are indexed by ``type(event)``. A subscriber to a base
    class must NOT receive a derived event, mirroring command dispatch.
    """

    def test_base_subscriber_does_not_receive_derived(self):
        bus = EventBus()
        base_seen: list[Event] = []
        derived_seen: list[Event] = []

        bus.subscribe(UserCreated, lambda e: base_seen.append(e))
        bus.subscribe(DerivedUserCreated, lambda e: derived_seen.append(e))

        bus.publish(DerivedUserCreated(user_id="u-1", extra="x"))

        assert base_seen == []
        assert len(derived_seen) == 1

    def test_derived_subscriber_does_not_receive_base(self):
        bus = EventBus()
        derived_seen: list[Event] = []

        bus.subscribe(DerivedUserCreated, lambda e: derived_seen.append(e))
        bus.publish(UserCreated(user_id="u-1"))

        assert derived_seen == []


# ---------------------------------------------------------------------------
# Subscribe input validation
# ---------------------------------------------------------------------------


class TestSubscribeValidation:
    def test_subscribe_rejects_non_event_class(self):
        bus = EventBus()
        with pytest.raises(TypeError):
            bus.subscribe(str, lambda _: None)  # type: ignore[arg-type]

    def test_subscribe_rejects_instance(self):
        bus = EventBus()
        with pytest.raises(TypeError):
            bus.subscribe(UserCreated(user_id="u-1"), lambda _: None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# @subscribes decorator + ModuleHost auto-wiring
# ---------------------------------------------------------------------------


@dataclass
class PingInput(CommandRequest):
    pass


@dataclass
class PingOutput(CommandResponse):
    pass


class PingCommand(Command[PingInput, PingOutput]):
    name = "test.ping"


class AuditModule(Module):
    def __init__(self) -> None:
        super().__init__()
        self.users: list[str] = []
        self.orders: list[str] = []

    @subscribes(UserCreated)
    def on_user(self, event: UserCreated) -> None:
        self.users.append(event.user_id)

    @subscribes(OrderPlaced)
    def on_order(self, event: OrderPlaced) -> None:
        self.orders.append(event.order_id)


class PublisherModule(Module):
    """Module that handles a Command and publishes an Event from inside it."""

    def __init__(self) -> None:
        super().__init__()
        self.host: ModuleHost | None = None

    @handles(PingCommand)
    def handle_ping(self, command: PingCommand) -> PingOutput:
        if self.host is not None:
            self.host.publish(UserCreated(user_id="u-from-ping"))
        return PingOutput()


class MultiSubscribeModule(Module):
    """One method subscribed to two Event classes via @subscribes(A, B)."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[Event] = []

    @subscribes(UserCreated, OrderPlaced)
    def on_any(self, event: Event) -> None:
        self.received.append(event)


class TestSubscribesDecorator:
    def test_decorator_marks_method(self):
        method = AuditModule.__dict__["on_user"]
        claims = getattr(method, SUBSCRIBES_ATTR)
        assert claims == (UserCreated,)

    def test_decorator_rejects_empty_args(self):
        with pytest.raises(TypeError):

            class _Bad(Module):
                @subscribes()  # type: ignore[call-arg]
                def listener(self, event: Event) -> None: ...

    def test_decorator_rejects_non_event_class(self):
        with pytest.raises(TypeError):

            class _Bad(Module):
                @subscribes(str)  # type: ignore[arg-type]
                def listener(self, event: Event) -> None: ...


class TestModuleHostAutoWiring:
    def test_subscribed_module_receives_events(self):
        host = ModuleHost()
        audit = AuditModule()
        host.register(audit)

        host.publish(UserCreated(user_id="u-1"))
        host.publish(OrderPlaced(order_id="o-9"))

        assert audit.users == ["u-1"]
        assert audit.orders == ["o-9"]
        host.shutdown()

    def test_module_handler_publishes_event_picked_up_by_subscriber(self):
        host = ModuleHost()
        audit = AuditModule()
        publisher = PublisherModule()
        publisher.host = host
        host.register(audit)
        host.register(publisher)

        host.dispatch(PingCommand(request=PingInput()))

        assert audit.users == ["u-from-ping"]
        host.shutdown()

    def test_unregister_removes_subscriptions(self):
        host = ModuleHost()
        audit = AuditModule()
        host.register(audit)
        host.unregister(audit)

        host.publish(UserCreated(user_id="u-1"))

        assert audit.users == []
        host.shutdown()

    def test_multi_event_subscribes_decorator(self):
        host = ModuleHost()
        mod = MultiSubscribeModule()
        host.register(mod)

        host.publish(UserCreated(user_id="u-1"))
        host.publish(OrderPlaced(order_id="o-9"))

        assert len(mod.received) == 2
        assert isinstance(mod.received[0], UserCreated)
        assert isinstance(mod.received[1], OrderPlaced)
        host.shutdown()

    def test_multiple_modules_subscribe_to_same_event(self):
        """Pub/sub fan-out: many Modules may subscribe to the same Event."""
        host = ModuleHost()
        audit_a = AuditModule()
        audit_b = AuditModule()
        host.register(audit_a)
        host.register(audit_b)

        host.publish(UserCreated(user_id="u-1"))

        assert audit_a.users == ["u-1"]
        assert audit_b.users == ["u-1"]
        host.shutdown()

    def test_event_bus_property_returns_same_instance(self):
        host = ModuleHost()
        assert host.event_bus is host.event_bus
        host.shutdown()

    def test_publish_async_on_host(self):
        host = ModuleHost()
        audit = AuditModule()
        host.register(audit)

        async def run() -> None:
            await host.publish_async(UserCreated(user_id="u-1"))

        asyncio.run(run())
        assert audit.users == ["u-1"]
        host.shutdown()


class TestEventNameClassAttribute:
    """Event subclasses can declare ``name`` as a class attribute, mirroring Command."""

    def test_default_name_uses_class_attribute(self):
        ev = UserCreated(user_id="u-1")
        assert ev.name == "user.created"

    def test_explicit_name_overrides_class_attribute(self):
        ev = UserCreated(user_id="u-1", name="custom.name")
        assert ev.name == "custom.name"
