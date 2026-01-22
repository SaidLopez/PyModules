"""
Greeter Module - Simple example of a module with typed input/output.

This demonstrates the basic pattern of creating events with
strongly typed input and output dataclasses.
"""

from dataclasses import dataclass

from pymodules import Event, EventInput, EventOutput, Module, module


@dataclass
class GreetInput(EventInput):
    """Input for greeting events."""

    name: str = "World"
    formal: bool = False


@dataclass
class GreetOutput(EventOutput):
    """Output from greeting - the generated message."""

    message: str = ""


class GreetEvent(Event[GreetInput, GreetOutput]):
    """Event requesting a greeting message."""

    name = "pymodules.greet"


@module(name="Greeter", description="Generates greeting messages", version="1.0.0")
class GreeterModule(Module):
    """
    A simple module that generates greeting messages.

    Example:
        host = ModuleHost()
        host.register(GreeterModule())

        event = GreetEvent(input=GreetInput(name="Alice"))
        host.handle(event)
        print(event.output.message)  # "Hello, Alice!"
    """

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, GreetEvent)

    def handle(self, event: Event) -> None:
        if not isinstance(event, GreetEvent):
            return

        inp = event.input
        if inp.formal:
            message = f"Good day, {inp.name}. How may I assist you?"
        else:
            message = f"Hello, {inp.name}!"

        event.output = GreetOutput(message=message)
        event.handled = True
