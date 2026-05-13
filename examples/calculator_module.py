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

        command = CalculatorCommand(input=CalculatorInput(a=1, b=2, operation="+"))
        host.dispatch(command)
        print(command.output.result)  # 3
    """

    @handles(CalculatorCommand)
    def calculate(self, command: CalculatorCommand) -> None:
        inp = command.input
        if inp.operation == "+":
            result = inp.a + inp.b
        elif inp.operation == "-":
            result = inp.a - inp.b
        elif inp.operation == "*":
            result = inp.a * inp.b
        elif inp.operation == "/":
            result = inp.a / inp.b
        else:
            command.handled = False
            return

        command.output = CalculatorOutput(result=result)
        command.handled = True
