"""
In-process EventBus for PyModules.

``Event`` and ``EventBus`` are the second dispatch primitive in PyModules,
sitting alongside ``Command`` / ``ModuleHost``. They are deliberately
separate because they have different semantics:

  - ``Command`` is dispatched to **exactly one** winning handler through a
    middleware chain and returns a typed response.
  - ``Event`` is published to **zero or more** subscribers, returns nothing,
    and a failure in one subscriber must not prevent others from receiving
    the notification.

Routing is exact-type, indexed by ``type(event)`` for O(1) lookup — the
same principle as the command dispatch table (see ADR-0003). There is no
predicate ``can_subscribe``; subscribers register for a concrete ``Event``
subclass and receive instances of that exact class. Inheritance fan-out
is intentionally **not** implemented — see ADR-0007 for the rationale.

Error isolation: ``publish`` is fire-and-forget. The publisher has no
return channel for subscriber errors, so each subscriber runs inside its
own ``try``/``except`` and exceptions are logged via ``eventbus_logger``
rather than propagated. A misbehaving subscriber cannot crash the
publisher or starve other subscribers.

Sync + async subscribers are both supported on both publish paths:

  - ``publish(event)`` is the sync facade. Sync subscribers run inline.
    Async subscribers are scheduled with ``asyncio.run`` on the
    publishing thread (a fresh loop per publish call); use
    ``publish_async`` from async contexts.
  - ``publish_async(event)`` runs in the caller's event loop. Sync
    subscribers run inline (the loop is single-threaded; offloading to a
    thread pool would be a contrib concern). Async subscribers are
    awaited sequentially in registration order.

The EventBus owns no broker or persistence — it is purely an in-process
fan-out registry. Persistent, ordered, or cross-process delivery is the
job of contrib brokers (Redis/Kafka) that Modules talk to directly.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .interfaces import Event
from .logging import eventbus_logger

# A subscriber callable receives an Event instance and returns either
# nothing (sync) or an awaitable that resolves to nothing (async).
EventHandler = Callable[[Event], None | Awaitable[None]]


class EventBus:
    """
    In-process pub/sub registry for ``Event`` instances.

    Subscribers register against a concrete ``Event`` subclass; ``publish``
    looks up the exact ``type(event)`` and invokes every registered
    subscriber. Subscriber errors are isolated — a raise in one subscriber
    is logged and swallowed; other subscribers still receive the event.

    Multiple ``EventBus`` instances may coexist; ``ModuleHost`` owns one by
    default (auto-wired to ``@subscribes``-decorated Module methods at
    registration time) but the class is independently usable.

    Example:
        @dataclass
        class UserCreated(Event):
            user_id: str = ""
            name: str = "user.created"

        bus = EventBus()

        def on_user_created(event: UserCreated) -> None:
            print(f"audit: user {event.user_id} created")

        bus.subscribe(UserCreated, on_user_created)
        bus.publish(UserCreated(user_id="u-123"))
    """

    def __init__(self) -> None:
        # Exact-type index: type(event) -> list of subscribers, in
        # registration order. List (not set) so registration order is
        # stable and a callable can subscribe twice deliberately if it
        # wants to (though that is rare).
        self._subscribers: dict[type[Event], list[EventHandler]] = {}

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, event_type: type[Event], handler: EventHandler) -> None:
        """
        Register ``handler`` to receive instances of ``event_type``.

        Routing is exact-type: a subscriber registered for ``BaseEvent``
        does **not** receive instances of ``DerivedEvent``. If you need
        that semantics, subscribe to each concrete class explicitly.

        The same handler may be subscribed multiple times; each
        registration produces one delivery per publish. ``unsubscribe``
        removes one registration at a time (first-match).
        """
        if not isinstance(event_type, type) or not issubclass(event_type, Event):
            raise TypeError(f"event_type must be a subclass of Event; got {event_type!r}")
        self._subscribers.setdefault(event_type, []).append(handler)
        eventbus_logger.debug(
            "Subscribed %s to %s (now %d subscriber(s))",
            getattr(handler, "__qualname__", repr(handler)),
            event_type.__name__,
            len(self._subscribers[event_type]),
        )

    def unsubscribe(self, event_type: type[Event], handler: EventHandler) -> bool:
        """
        Remove the first registration of ``handler`` for ``event_type``.

        Returns ``True`` if a registration was removed, ``False`` if no
        matching registration existed.
        """
        handlers = self._subscribers.get(event_type)
        if not handlers:
            return False
        try:
            handlers.remove(handler)
        except ValueError:
            return False
        if not handlers:
            # Drop the empty bucket so ``has_subscribers`` reflects reality.
            del self._subscribers[event_type]
        eventbus_logger.debug(
            "Unsubscribed %s from %s",
            getattr(handler, "__qualname__", repr(handler)),
            event_type.__name__,
        )
        return True

    def has_subscribers(self, event_type: type[Event]) -> bool:
        """True if at least one subscriber is registered for ``event_type``."""
        return bool(self._subscribers.get(event_type))

    def subscriber_count(self, event_type: type[Event]) -> int:
        """Number of subscribers registered for ``event_type``."""
        return len(self._subscribers.get(event_type, ()))

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, event: Event) -> None:
        """
        Synchronously publish ``event`` to every exact-type subscriber.

        Sync subscribers run inline on the calling thread, in registration
        order. Async subscribers are bridged via ``asyncio.run`` — one
        fresh loop per ``publish`` call. If a loop is already running in
        this thread (i.e., you are inside an ``async def``), use
        ``publish_async`` instead; calling sync ``publish`` from inside a
        running loop will raise ``RuntimeError`` from ``asyncio.run``.

        All subscriber exceptions are caught and logged; this method
        never raises on behalf of a subscriber.
        """
        handlers = list(self._subscribers.get(type(event), ()))
        if not handlers:
            eventbus_logger.debug(
                "No subscribers for %s (event name=%r); dropping",
                type(event).__name__,
                event.name,
            )
            return

        eventbus_logger.debug(
            "Publishing %s to %d subscriber(s)", type(event).__name__, len(handlers)
        )
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    # Bridge async subscribers from sync publish. A fresh
                    # loop per call keeps semantics simple — there is no
                    # ambient loop on the sync path.
                    asyncio.run(_await_result(result))
            except Exception:  # noqa: BLE001 — error isolation is the point
                eventbus_logger.exception(
                    "Subscriber %s raised while handling %s; isolating",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(event).__name__,
                )

    async def publish_async(self, event: Event) -> None:
        """
        Asynchronously publish ``event`` to every exact-type subscriber.

        Subscribers are invoked sequentially in registration order. Async
        subscribers are awaited; sync subscribers run inline on the
        calling task (kept simple — offloading to a thread pool would be
        a contrib concern, and most in-process subscribers are cheap
        bookkeeping callbacks).

        All subscriber exceptions are caught and logged.
        """
        handlers = list(self._subscribers.get(type(event), ()))
        if not handlers:
            eventbus_logger.debug(
                "No subscribers for %s (event name=%r); dropping",
                type(event).__name__,
                event.name,
            )
            return

        eventbus_logger.debug(
            "Publishing %s async to %d subscriber(s)",
            type(event).__name__,
            len(handlers),
        )
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — error isolation is the point
                eventbus_logger.exception(
                    "Subscriber %s raised while handling %s; isolating",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(event).__name__,
                )

    def clear(self) -> None:
        """Remove every subscription. Primarily useful in tests."""
        self._subscribers.clear()


async def _await_result(awaitable: Any) -> None:
    """Trivial coroutine wrapper so we can pass any awaitable to ``asyncio.run``."""
    await awaitable


__all__ = ["EventBus", "EventHandler"]
