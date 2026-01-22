"""
Core interfaces for PyModules event system.

Events are typed messages with Input data and Output response.
They flow through the ModuleHost to be handled by Modules.
"""

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


@dataclass
class EventInput:
    """
    Base class for event input data.

    Subclass this to define the input parameters for your event.

    Example:
        @dataclass
        class GreetInput(EventInput):
            name: str
    """

    pass


@dataclass
class EventOutput:
    """
    Base class for event output data.

    Subclass this to define the return data from your event handler.

    Example:
        @dataclass
        class GreetOutput(EventOutput):
            message: str
    """

    pass


# Type variables for generic Event (single letters are conventional for TypeVars)
I = TypeVar("I", bound=EventInput)  # noqa: E741
O = TypeVar("O", bound=EventOutput)  # noqa: E741


@dataclass
class Event(Generic[I, O]):
    """
    An event that can be dispatched through a ModuleHost.

    Events carry input data to handlers and receive output data back.
    The `handled` flag indicates if a module successfully processed the event.

    Attributes:
        name: Unique identifier for this event type (e.g., "com.example.greet")
        input: Input data for the event handler
        output: Output data set by the handler
        handled: True if a module successfully handled this event
        meta: Additional metadata that can be passed between modules

    Example:
        @dataclass
        class GreetInput(EventInput):
            name: str

        @dataclass
        class GreetOutput(EventOutput):
            message: str

        class GreetEvent(Event[GreetInput, GreetOutput]):
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
