"""
Demo script showing PyModules in action.

Run with: python -m examples.demo
"""

from examples.calculator_module import (
    CalculatorCommand,
    CalculatorInput,
    CalculatorModule,
)
from examples.greet_module import GreetCommand, GreeterModule, GreetInput
from examples.logging_module import LoggingCommand, LoggingInput, LoggingModule, LogLevel
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
    greet = GreetCommand(request=GreetInput(name="World"))
    greet_response = host.dispatch(greet)
    print(f"Result: {greet_response.message}")

    # Example 2: Formal greeting
    print("\n--- Example 2: Formal Greeting ---")
    formal_greet = GreetCommand(request=GreetInput(name="Dr. Smith", formal=True))
    formal_response = host.dispatch(formal_greet)
    print(f"Result: {formal_response.message}")

    # Example 3: Logging (cross-cutting concern)
    print("\n--- Example 3: Logging Command ---")
    log = LoggingCommand(
        request=LoggingInput(
            level=LogLevel.INFO,
            message="User {} performed action: {}",
            args=["alice", "login"],
        )
    )
    host.dispatch(log)

    # Example 4: Module dispatching commands to other modules
    print("\n--- Example 4: Module-to-Module Communication ---")
    print("(The GreeterModule could log via LoggingCommand through host)")

    # Show that modules can access the host
    greeter = host.get_module_by_name("Greeter")
    if greeter and greeter.host:
        log_cmd = LoggingCommand(
            request=LoggingInput(level=LogLevel.DEBUG, message="Greeter module is active")
        )
        greeter.host.dispatch(log_cmd)

    # Example 5: Calculator
    print("\n--- Example 5: Calculator ---")
    calc = CalculatorCommand(request=CalculatorInput(a=1, b=2, operation="+"))
    calc_response = host.dispatch(calc)
    calc_log_cmd = LoggingCommand(
        request=LoggingInput(
            level=LogLevel.INFO, message=f"Calculator result: {calc_response.result}"
        )
    )
    host.dispatch(calc_log_cmd)

    print("\n" + "=" * 50)
    print("Demo complete!")
    print("=" * 50)


if __name__ == "__main__":
    main()
