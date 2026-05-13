"""
Greeter Module - Simple example of a module with typed request/response.

This demonstrates the basic pattern of creating commands with
strongly typed request and response dataclasses.
"""

from dataclasses import dataclass

from pymodules import Command, CommandRequest, CommandResponse, Module, handles, module


@dataclass
class GreetInput(CommandRequest):
    """Request payload for greeting commands."""

    name: str = "World"
    formal: bool = False


@dataclass
class GreetOutput(CommandResponse):
    """Response from greeting - the generated message."""

    message: str = ""


class GreetCommand(Command[GreetInput, GreetOutput]):
    """Command requesting a greeting message."""

    name = "pymodules.greet"


@module(name="Greeter", description="Generates greeting messages", version="1.0.0")
class GreeterModule(Module):
    """
    A simple module that generates greeting messages.

    Example:
        host = ModuleHost()
        host.register(GreeterModule())

        command = GreetCommand(input=GreetInput(name="Alice"))
        host.dispatch(command)
        print(command.output.message)  # "Hello, Alice!"
    """

    @handles(GreetCommand)
    def greet(self, command: GreetCommand) -> None:
        inp = command.input
        if inp.formal:
            message = f"Good day, {inp.name}. How may I assist you?"
        else:
            message = f"Hello, {inp.name}!"

        command.output = GreetOutput(message=message)
        command.handled = True
