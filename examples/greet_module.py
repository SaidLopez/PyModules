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

        command = GreetCommand(request=GreetInput(name="Alice"))
        response = host.dispatch(command)
        print(response.message)  # "Hello, Alice!"
    """

    @handles(GreetCommand)
    def greet(self, command: GreetCommand) -> GreetOutput:
        req = command.request
        if req.formal:
            message = f"Good day, {req.name}. How may I assist you?"
        else:
            message = f"Hello, {req.name}!"

        return GreetOutput(message=message)
