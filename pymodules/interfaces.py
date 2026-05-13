"""
Core interfaces for the PyModules command-dispatch system.

Commands are typed in-process requests with a CommandRequest payload and a
CommandResponse. They flow through the ModuleHost and are dispatched to
exactly one claiming Module.
"""

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


@dataclass
class CommandRequest:
    """
    Base class for a command's typed request payload.

    Subclass this to define the input parameters for your command.

    Example:
        @dataclass
        class GreetRequest(CommandRequest):
            name: str
    """

    pass


@dataclass
class CommandResponse:
    """
    Base class for a command's typed response.

    Subclass this to define the value returned by your command handler.

    Example:
        @dataclass
        class GreetResponse(CommandResponse):
            message: str
    """

    pass


# Type variables for generic Command (single letters are conventional for TypeVars)
I = TypeVar("I", bound=CommandRequest)  # noqa: E741
O = TypeVar("O", bound=CommandResponse)  # noqa: E741


@dataclass
class Command(Generic[I, O]):
    """
    A command that can be dispatched through a ModuleHost.

    Commands carry a typed request into the claiming Module and (today)
    receive a typed response set on ``output``. The ``handled`` flag
    indicates whether a Module successfully processed the command.

    Attributes:
        name: Unique identifier for this command type (e.g., "com.example.greet")
        input: Request data passed to the handler
        output: Response data set by the handler
        handled: True if a Module successfully handled this command
        meta: Additional metadata that can be passed between Modules

    Example:
        @dataclass
        class GreetRequest(CommandRequest):
            name: str

        @dataclass
        class GreetResponse(CommandResponse):
            message: str

        class GreetCommand(Command[GreetRequest, GreetResponse]):
            name = "example.greet"
    """

    name: str = ""
    input: I | None = None
    output: O | None = None
    handled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Allow subclasses to define name as class attribute
        if not self.name and hasattr(self.__class__, "name"):
            class_name = self.__class__.name
            if isinstance(class_name, str) and class_name:
                object.__setattr__(self, "name", class_name)
