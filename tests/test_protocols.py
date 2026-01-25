"""Tests for Protocol classes enabling structural typing."""

import asyncio

from pymodules.protocols import AsyncEventHandler, EventHandler, EventLike


def test_event_like_protocol():
    """Verify protocol accepts duck-typed events."""

    class CustomEvent:
        name = "custom"
        input = None
        output = None
        handled = False
        meta = {}

    def process(event: EventLike) -> None:
        event.handled = True

    custom = CustomEvent()
    process(custom)
    assert custom.handled is True


def test_event_handler_protocol():
    """Verify protocol accepts duck-typed handlers."""

    class CustomHandler:
        def can_handle(self, event) -> bool:
            return True

        def handle(self, event) -> None:
            event.handled = True

    def dispatch(handler: EventHandler, event: EventLike) -> None:
        if handler.can_handle(event):
            handler.handle(event)

    handler = CustomHandler()
    event = type(
        "E", (), {"name": "x", "input": None, "output": None, "handled": False, "meta": {}}
    )()
    dispatch(handler, event)
    assert event.handled is True


def test_event_like_isinstance_check():
    """Verify runtime_checkable works with isinstance."""

    class CustomEvent:
        name = "custom"
        input = None
        output = None
        handled = False
        meta = {}

    custom = CustomEvent()
    assert isinstance(custom, EventLike)


def test_event_handler_isinstance_check():
    """Verify runtime_checkable works with isinstance for handlers."""

    class CustomHandler:
        def can_handle(self, event) -> bool:
            return True

        def handle(self, event) -> None:
            pass

    handler = CustomHandler()
    assert isinstance(handler, EventHandler)


def test_non_conforming_event_fails_isinstance():
    """Verify objects missing required attributes fail isinstance check."""

    class IncompleteEvent:
        name = "incomplete"
        # Missing: input, output, handled, meta

    incomplete = IncompleteEvent()
    assert not isinstance(incomplete, EventLike)


def test_non_conforming_handler_fails_isinstance():
    """Verify objects missing required methods fail isinstance check."""

    class IncompleteHandler:
        def can_handle(self, event) -> bool:
            return True

        # Missing: handle method

    incomplete = IncompleteHandler()
    assert not isinstance(incomplete, EventHandler)


def test_async_event_handler_isinstance_check():
    """Verify AsyncEventHandler isinstance check works."""

    class AsyncHandler:
        def can_handle(self, event) -> bool:
            return True

        async def handle(self, event) -> None:
            event.handled = True

    handler = AsyncHandler()
    assert isinstance(handler, AsyncEventHandler)


def test_async_event_handler_dispatch():
    """Verify async handler can be dispatched."""

    class AsyncHandler:
        def can_handle(self, event) -> bool:
            return True

        async def handle(self, event) -> None:
            await asyncio.sleep(0)  # Simulate async operation
            event.handled = True

    async def dispatch(handler, event):
        if handler.can_handle(event):
            await handler.handle(event)

    handler = AsyncHandler()
    event = type(
        "E", (), {"name": "x", "input": None, "output": None, "handled": False, "meta": {}}
    )()

    asyncio.run(dispatch(handler, event))
    assert event.handled is True
