"""
This is a calculator module as a simple example
"""

from dataclasses import dataclass

from pymodules import Event, EventInput, EventOutput, Module, module


@dataclass
class CalculatorInput(EventInput):
    """Input for calculator events."""

    a: int = 0
    b: int = 0
    operation: str = "+"


@dataclass
class CalculatorOutput(EventOutput):
    """Output from calculator - the result of the operation."""

    result: int = 0


class CalculatorEvent(Event[CalculatorInput, CalculatorOutput]):
    """Event requesting a calculator operation."""

    name = "pymodules.calculator"


@module(name="Calculator", description="Calculator module", version="1.0.0")
class CalculatorModule(Module):
    """
    A simple module that performs calculator operations.

    Example:
        host = ModuleHost()
        host.register(CalculatorModule())

        event = CalculatorEvent(input=CalculatorInput(a=1, b=2, operation="+"))
        host.handle(event)
        print(event.output.result)  # 3
    """

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, CalculatorEvent)

    def handle(self, event: Event) -> None:
        if not isinstance(event, CalculatorEvent):
            return

        inp = event.input
        if inp.operation == "+":
            result = inp.a + inp.b
        elif inp.operation == "-":
            result = inp.a - inp.b
        elif inp.operation == "*":
            result = inp.a * inp.b
        elif inp.operation == "/":
            result = inp.a / inp.b
        else:
            event.handled = False
            return

        event.output = CalculatorOutput(result=result)
        event.handled = True
