"""
Protocol classes for structural typing in PyModules.

These protocols allow duck typing without requiring inheritance,
enabling interoperability with third-party code.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventLike(Protocol):
    """Protocol for event-like objects.

    Any object with these attributes can be used as an event,
    without needing to inherit from Event.

    Attributes:
        name: Unique identifier for this event type
        input: Input data for the event handler
        output: Output data set by the handler
        handled: True if a module successfully handled this event
        meta: Additional metadata dictionary
    """

    name: str
    input: Any
    output: Any
    handled: bool
    meta: dict[str, Any]


@runtime_checkable
class EventHandler(Protocol):
    """Protocol for objects that can handle events.

    Any object with can_handle and handle methods can be used
    as an event handler, without needing to inherit from Module.
    """

    def can_handle(self, event: EventLike) -> bool:
        """Return True if this handler can process the event."""
        ...

    def handle(self, event: EventLike) -> None:
        """Process the event."""
        ...


@runtime_checkable
class AsyncEventHandler(Protocol):
    """Protocol for async event handlers.

    Any object with can_handle and async handle methods can be used
    as an async event handler.
    """

    def can_handle(self, event: EventLike) -> bool:
        """Return True if this handler can process the event."""
        ...

    async def handle(self, event: EventLike) -> None:
        """Process the event asynchronously."""
        ...
