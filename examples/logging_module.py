"""
Logging Module - Example of a cross-cutting concern module.

This demonstrates how a single module can handle logging events
from multiple other modules, following the "deferred responsibility"
pattern from NetModules.
"""

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pymodules import Event, EventInput, EventOutput, Module, module


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class LoggingInput(EventInput):
    """Input for logging events."""

    level: LogLevel = LogLevel.INFO
    message: str = ""
    args: list[Any] = None

    def __post_init__(self):
        if self.args is None:
            self.args = []


@dataclass
class LoggingOutput(EventOutput):
    """Output from logging - indicates if log was written."""

    logged: bool = False


class LoggingEvent(Event[LoggingInput, LoggingOutput]):
    """Event for logging messages through the module system."""

    name = "pymodules.logging"


@module(
    name="ConsoleLogger", description="Logs messages to the console", version="1.0.0"
)
class LoggingModule(Module):
    """
    A module that handles logging events and outputs to console.

    This demonstrates the "deferred responsibility" pattern:
    other modules can raise LoggingEvents without knowing
    how or where the logs will be written.

    Example:
        # In any other module's handle() method:
        log_event = LoggingEvent(input=LoggingInput(
            level=LogLevel.INFO,
            message="User {} logged in",
            args=["john"]
        ))
        self.host.handle(log_event)
    """

    def __init__(self, output=None):
        super().__init__()
        self.output = output or sys.stdout

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, LoggingEvent)

    def handle(self, event: Event) -> None:
        if not isinstance(event, LoggingEvent):
            return

        inp = event.input
        message = inp.message
        if inp.args:
            message = message.format(*inp.args)

        log_line = f"[{inp.level.value}] {message}\n"
        self.output.write(log_line)
        self.output.flush()

        event.output = LoggingOutput(logged=True)
        event.handled = True
