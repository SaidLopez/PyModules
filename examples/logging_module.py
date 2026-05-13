"""
Logging Module - Example of a cross-cutting concern module.

This demonstrates how a single module can handle logging commands
from multiple other modules, following the "deferred responsibility"
pattern from NetModules.
"""

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pymodules import Command, CommandRequest, CommandResponse, Module, handles, module


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class LoggingInput(CommandRequest):
    """Request payload for logging commands."""

    level: LogLevel = LogLevel.INFO
    message: str = ""
    args: list[Any] = None

    def __post_init__(self):
        if self.args is None:
            self.args = []


@dataclass
class LoggingOutput(CommandResponse):
    """Response from logging - indicates if log was written."""

    logged: bool = False


class LoggingCommand(Command[LoggingInput, LoggingOutput]):
    """Command for logging messages through the module system."""

    name = "pymodules.logging"


@module(name="ConsoleLogger", description="Logs messages to the console", version="1.0.0")
class LoggingModule(Module):
    """
    A module that handles logging commands and outputs to console.

    This demonstrates the "deferred responsibility" pattern:
    other modules can dispatch LoggingCommands without knowing
    how or where the logs will be written.

    Example:
        # In any other module's handle() method:
        log_command = LoggingCommand(request=LoggingInput(
            level=LogLevel.INFO,
            message="User {} logged in",
            args=["john"]
        ))
        self.host.dispatch(log_command)
    """

    def __init__(self, output=None):
        super().__init__()
        self.output = output or sys.stdout

    @handles(LoggingCommand)
    def log(self, command: LoggingCommand) -> LoggingOutput:
        req = command.request
        message = req.message
        if req.args:
            message = message.format(*req.args)

        log_line = f"[{req.level.value}] {message}\n"
        self.output.write(log_line)
        self.output.flush()

        return LoggingOutput(logged=True)
