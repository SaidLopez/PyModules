"""
Core interfaces for the PyModules command-dispatch system.

Commands are typed in-process requests with a CommandRequest payload and a
CommandResponse. They flow through the ModuleHost and are dispatched to
exactly one claiming Module. The claiming Module **returns** its
CommandResponse; the Command itself is no longer mutated to carry the result.
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


# Type variables for generic Command. ``Req`` is the request payload type
# carried into the handler; ``Resp`` is the response type the handler returns
# (propagated to ``ModuleHost.dispatch``'s return annotation).
Req = TypeVar("Req", bound=CommandRequest)
Resp = TypeVar("Resp", bound=CommandResponse)


@dataclass
class Command(Generic[Req, Resp]):
    """
    A command that can be dispatched through a ModuleHost.

    Commands carry a typed request into the claiming Module. The Module
    **returns** its typed response from the decorated handler; the Command
    is not mutated.

    Attributes:
        name: Unique identifier for this command type (e.g., "com.example.greet")
        request: Request data passed to the handler
        meta: Additional metadata that can be passed between Modules

    The ``Resp`` type parameter exists for static typing only: it lets
    ``ModuleHost.dispatch(cmd: Command[Req, Resp]) -> Resp`` propagate the
    response type to the caller. There is no runtime ``response`` field.

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
    request: Req | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Allow subclasses to define name as class attribute
        if not self.name and hasattr(self.__class__, "name"):
            class_name = self.__class__.name
            if isinstance(class_name, str) and class_name:
                object.__setattr__(self, "name", class_name)
