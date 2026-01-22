"""
Demo script showing PyModules in action.

Run with: python -m examples.demo
"""

from examples.calculator_module import (
    CalculatorEvent,
    CalculatorInput,
    CalculatorModule,
)
from examples.greet_module import GreeterModule, GreetEvent, GreetInput
from examples.logging_module import LoggingEvent, LoggingInput, LoggingModule, LogLevel
from pymodules import ModuleHost


def main():
    print("=" * 50)
    print("PyModules Demo")
    print("=" * 50)

    # Create a ModuleHost and register modules
    host = ModuleHost()
    host.register(LoggingModule())
    host.register(GreeterModule())
    host.register(CalculatorModule())

    print(f"\nRegistered {len(host.modules)} modules:")
    for m in host.modules:
        print(f"  - {m.metadata.name}: {m.metadata.description}")

    # Example 1: Simple greeting
    print("\n--- Example 1: Simple Greeting ---")
    greet = GreetEvent(input=GreetInput(name="World"))
    host.handle(greet)
    print(f"Result: {greet.output.message}")
    print(f"Handled: {greet.handled}")

    # Example 2: Formal greeting
    print("\n--- Example 2: Formal Greeting ---")
    formal_greet = GreetEvent(input=GreetInput(name="Dr. Smith", formal=True))
    host.handle(formal_greet)
    print(f"Result: {formal_greet.output.message}")

    # Example 3: Logging (cross-cutting concern)
    print("\n--- Example 3: Logging Event ---")
    log = LoggingEvent(
        input=LoggingInput(
            level=LogLevel.INFO,
            message="User {} performed action: {}",
            args=["alice", "login"],
        )
    )
    host.handle(log)

    # Example 4: Module dispatching events to other modules
    print("\n--- Example 4: Module-to-Module Communication ---")
    print("(The GreeterModule could log via LoggingEvent through host)")

    # Show that modules can access the host
    greeter = host.get_module_by_name("Greeter")
    if greeter and greeter.host:
        log_event = LoggingEvent(
            input=LoggingInput(level=LogLevel.DEBUG, message="Greeter module is active")
        )
        greeter.host.handle(log_event)

    # Example 5: Calculator
    print("\n--- Example 5: Calculator ---")
    calc = CalculatorEvent(input=CalculatorInput(a=1, b=2, operation="+"))
    host.handle(calc)
    calc_log_event = LoggingEvent(
        input=LoggingInput(
            level=LogLevel.INFO, message=f"Calculator result: {calc.output.result}"
        )
    )
    host.handle(calc_log_event)

    print("\n" + "=" * 50)
    print("Demo complete!")
    print("=" * 50)


if __name__ == "__main__":
    main()
