"""
Protocol classes for structural typing in PyModules.

These protocols allow duck typing without requiring inheritance,
enabling interoperability with third-party code.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CommandLike(Protocol):
    """Protocol for command-like objects.

    Any object with these attributes can be used as a command,
    without needing to inherit from Command.

    Attributes:
        name: Unique identifier for this command type
        input: Request data passed to the handler
        output: Response data set by the handler
        handled: True if a Module successfully handled this command
        meta: Additional metadata dictionary
    """

    name: str
    input: Any
    output: Any
    handled: bool
    meta: dict[str, Any]


@runtime_checkable
class CommandHandler(Protocol):
    """Protocol for objects that can handle commands.

    Any object with can_handle and handle methods can be used
    as a command handler, without needing to inherit from Module.
    """

    def can_handle(self, command: CommandLike) -> bool:
        """Return True if this handler can process the command."""
        ...

    def handle(self, command: CommandLike) -> None:
        """Process the command."""
        ...


@runtime_checkable
class AsyncCommandHandler(Protocol):
    """Protocol for async command handlers.

    Any object with can_handle and async handle methods can be used
    as an async command handler.
    """

    def can_handle(self, command: CommandLike) -> bool:
        """Return True if this handler can process the command."""
        ...

    async def handle(self, command: CommandLike) -> None:
        """Process the command asynchronously."""
        ...
