"""
Module base class for PyModules.

Modules are event handlers that declare what events they can process.
They are registered with a ModuleHost and receive events through the
can_handle/handle pattern.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .interfaces import Event

if TYPE_CHECKING:
    from .host import ModuleHost


@dataclass
class ModuleMetadata:
    """Metadata for a module."""

    name: str = ""
    description: str = ""
    version: str = "1.0.0"


def module(
    name: str = "", description: str = "", version: str = "1.0.0"
) -> Callable[[type], type]:
    """
    Decorator to add metadata to a module class.

    Example:
        @module(name="Greeter", description="Handles greeting events")
        class GreeterModule(Module):
            ...
    """

    def decorator(cls: type) -> type:
        # Set metadata on the class (Module classes have this attribute)
        cls._module_metadata = ModuleMetadata(  # type: ignore[attr-defined]
            name=name or cls.__name__, description=description, version=version
        )
        return cls

    return decorator


class Module(ABC):
    """
    Abstract base class for event handler modules.

    Subclass this to create a module that handles specific event types.
    Override can_handle() to declare which events you support, and
    handle() to process those events.

    Each module has access to its host via the `host` property,
    allowing it to dispatch events to other modules.

    Example:
        @module(name="Greeter", description="Handles greeting events")
        class GreeterModule(Module):
            def can_handle(self, event: Event) -> bool:
                return isinstance(event, GreetEvent)

            def handle(self, event: Event) -> None:
                if isinstance(event, GreetEvent):
                    event.output = GreetOutput(
                        message=f"Hello, {event.input.name}!"
                    )
                    event.handled = True
    """

    _module_metadata: ModuleMetadata = ModuleMetadata()

    def __init__(self) -> None:
        self._host: ModuleHost | None = None
        # Initialize metadata from class if not set by decorator
        if not hasattr(self.__class__, "_module_metadata"):
            self.__class__._module_metadata = ModuleMetadata(name=self.__class__.__name__)

    @property
    def host(self) -> Optional["ModuleHost"]:
        """The ModuleHost this module is registered with."""
        return self._host

    @property
    def metadata(self) -> ModuleMetadata:
        """Module metadata (name, description, version)."""
        return self.__class__._module_metadata

    @abstractmethod
    def can_handle(self, event: Event) -> bool:
        """
        Return True if this module can handle the given event.

        This is called by ModuleHost before handle() to determine
        if this module should process the event.

        Args:
            event: The event to check

        Returns:
            True if this module can handle the event
        """
        pass

    @abstractmethod
    def handle(self, event: Event) -> None:
        """
        Process the event.

        Set event.output with the result and event.handled = True
        to indicate successful processing. If handled is True,
        no other modules will receive this event.

        Args:
            event: The event to handle
        """
        pass

    def on_load(self) -> None:
        """Called when the module is loaded into a host."""
        pass

    def on_unload(self) -> None:
        """Called when the module is unloaded from a host."""
        pass
