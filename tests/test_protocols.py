"""Tests for Protocol classes enabling structural typing."""

import asyncio

from pymodules.protocols import AsyncCommandHandler, CommandHandler, CommandLike


def test_command_like_protocol():
    """Verify protocol accepts duck-typed commands."""

    class CustomCommand:
        name = "custom"
        input = None
        output = None
        handled = False
        meta = {}

    def process(command: CommandLike) -> None:
        command.handled = True

    custom = CustomCommand()
    process(custom)
    assert custom.handled is True


def test_command_handler_protocol():
    """Verify protocol accepts duck-typed handlers."""

    class CustomHandler:
        def can_handle(self, command) -> bool:
            return True

        def handle(self, command) -> None:
            command.handled = True

    def dispatch(handler: CommandHandler, command: CommandLike) -> None:
        if handler.can_handle(command):
            handler.handle(command)

    handler = CustomHandler()
    command = type(
        "C", (), {"name": "x", "input": None, "output": None, "handled": False, "meta": {}}
    )()
    dispatch(handler, command)
    assert command.handled is True


def test_command_like_isinstance_check():
    """Verify runtime_checkable works with isinstance."""

    class CustomCommand:
        name = "custom"
        input = None
        output = None
        handled = False
        meta = {}

    custom = CustomCommand()
    assert isinstance(custom, CommandLike)


def test_command_handler_isinstance_check():
    """Verify runtime_checkable works with isinstance for handlers."""

    class CustomHandler:
        def can_handle(self, command) -> bool:
            return True

        def handle(self, command) -> None:
            pass

    handler = CustomHandler()
    assert isinstance(handler, CommandHandler)


def test_non_conforming_command_fails_isinstance():
    """Verify objects missing required attributes fail isinstance check."""

    class IncompleteCommand:
        name = "incomplete"
        # Missing: input, output, handled, meta

    incomplete = IncompleteCommand()
    assert not isinstance(incomplete, CommandLike)


def test_non_conforming_handler_fails_isinstance():
    """Verify objects missing required methods fail isinstance check."""

    class IncompleteHandler:
        def can_handle(self, command) -> bool:
            return True

        # Missing: handle method

    incomplete = IncompleteHandler()
    assert not isinstance(incomplete, CommandHandler)


def test_async_command_handler_isinstance_check():
    """Verify AsyncCommandHandler isinstance check works."""

    class AsyncHandler:
        def can_handle(self, command) -> bool:
            return True

        async def handle(self, command) -> None:
            command.handled = True

    handler = AsyncHandler()
    assert isinstance(handler, AsyncCommandHandler)


def test_async_command_handler_dispatch():
    """Verify async handler can be dispatched."""

    class AsyncHandler:
        def can_handle(self, command) -> bool:
            return True

        async def handle(self, command) -> None:
            await asyncio.sleep(0)  # Simulate async operation
            command.handled = True

    async def dispatch(handler, command):
        if handler.can_handle(command):
            await handler.handle(command)

    handler = AsyncHandler()
    command = type(
        "C", (), {"name": "x", "input": None, "output": None, "handled": False, "meta": {}}
    )()

    asyncio.run(dispatch(handler, command))
    assert command.handled is True
