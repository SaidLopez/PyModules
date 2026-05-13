"""
Exception classes for PyModules framework.

Provides typed exceptions for better error handling and debugging.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .interfaces import Command
    from .module import Module


class PyModulesError(Exception):
    """Base exception for all PyModules errors."""

    pass


class CommandHandlingError(PyModulesError):
    """
    Raised when an error occurs during command handling.

    Attributes:
        command: The command that was being processed.
        module: The Module that raised the error (if known).
        original_error: The original exception that was caught.
    """

    def __init__(
        self,
        message: str,
        command: Optional["Command"] = None,
        module: Optional["Module"] = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.command = command
        self.module = module
        self.original_error = original_error

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.command:
            parts.append(f"Command: {self.command.name}")
        if self.module:
            parts.append(f"Module: {self.module.metadata.name}")
        if self.original_error:
            parts.append(f"Cause: {type(self.original_error).__name__}: {self.original_error}")
        return " | ".join(parts)


class ModuleRegistrationError(PyModulesError):
    """Raised when module registration fails."""

    pass


class DuplicateCommandError(ModuleRegistrationError):
    """
    Raised when a Module's ``@handles`` claim collides with an
    already-registered Module's claim.

    Pass ``override=True`` to ``ModuleHost.register`` to permit deliberate
    replacement (e.g., test doubles).
    """

    pass


class ConfigurationError(PyModulesError):
    """Raised when configuration is invalid."""

    pass


class SyncDispatchOnAsyncHandlerError(PyModulesError):
    """
    Raised by sync ``ModuleHost.dispatch()`` when the resolved handler is a
    coroutine function.

    There is no implicit ``asyncio.run()`` bridging — the caller must use
    ``dispatch_async()`` for async handlers.
    """

    pass


class SyncDispatchInAsyncContextError(PyModulesError):
    """
    Raised by sync ``ModuleHost.dispatch()`` when an event loop is already
    running in the current thread.

    Sync dispatch never starts or joins a running loop. The caller must use
    ``dispatch_async()`` from async contexts.
    """

    pass


# Database layer exceptions


class DatabaseError(PyModulesError):
    """Base exception for database-related errors."""

    pass


class ConnectionError(DatabaseError):
    """Raised when database connection fails."""

    pass


class RepositoryError(DatabaseError):
    """Raised when repository operations fail."""

    def __init__(
        self,
        message: str,
        model_name: str | None = None,
        operation: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.model_name = model_name
        self.operation = operation
        self.original_error = original_error

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.model_name:
            parts.append(f"Model: {self.model_name}")
        if self.operation:
            parts.append(f"Operation: {self.operation}")
        if self.original_error:
            parts.append(f"Cause: {type(self.original_error).__name__}: {self.original_error}")
        return " | ".join(parts)
