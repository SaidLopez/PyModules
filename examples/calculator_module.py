"""
This is a calculator module as a simple example
"""

from dataclasses import dataclass

from pymodules import Command, CommandRequest, CommandResponse, Module, handles, module


@dataclass
class CalculatorInput(CommandRequest):
    """Request for calculator commands."""

    a: int = 0
    b: int = 0
    operation: str = "+"


@dataclass
class CalculatorOutput(CommandResponse):
    """Response from calculator - the result of the operation."""

    result: int = 0


class CalculatorCommand(Command[CalculatorInput, CalculatorOutput]):
    """Command requesting a calculator operation."""

    name = "pymodules.calculator"


@module(name="Calculator", description="Calculator module", version="1.0.0")
class CalculatorModule(Module):
    """
    A simple module that performs calculator operations.

    Example:
        host = ModuleHost()
        host.register(CalculatorModule())

        command = CalculatorCommand(request=CalculatorInput(a=1, b=2, operation="+"))
        response = host.dispatch(command)
        print(response.result)  # 3
    """

    @handles(CalculatorCommand)
    def calculate(self, command: CalculatorCommand) -> CalculatorOutput:
        req = command.request
        if req.operation == "+":
            result = req.a + req.b
        elif req.operation == "-":
            result = req.a - req.b
        elif req.operation == "*":
            result = req.a * req.b
        elif req.operation == "/":
            result = req.a / req.b
        else:
            raise ValueError(f"Unsupported operation: {req.operation}")

        return CalculatorOutput(result=result)
