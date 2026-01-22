"""
Exception classes for PyModules framework.

Provides typed exceptions for better error handling and debugging.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .interfaces import Event
    from .module import Module


class PyModulesError(Exception):
    """Base exception for all PyModules errors."""

    pass


class EventHandlingError(PyModulesError):
    """
    Raised when an error occurs during event handling.

    Attributes:
        event: The event that was being processed.
        module: The module that raised the error (if known).
        original_error: The original exception that was caught.
    """

    def __init__(
        self,
        message: str,
        event: Optional["Event"] = None,
        module: Optional["Module"] = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.event = event
        self.module = module
        self.original_error = original_error

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.event:
            parts.append(f"Event: {self.event.name}")
        if self.module:
            parts.append(f"Module: {self.module.metadata.name}")
        if self.original_error:
            parts.append(f"Cause: {type(self.original_error).__name__}: {self.original_error}")
        return " | ".join(parts)


class ModuleRegistrationError(PyModulesError):
    """Raised when module registration fails."""

    pass


class ConfigurationError(PyModulesError):
    """Raised when configuration is invalid."""

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
