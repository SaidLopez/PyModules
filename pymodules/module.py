"""
Module base class for PyModules.

Modules are command handlers whose methods are individually decorated with
``@handles(CommandClass)`` to claim Commands. ``ModuleHost`` scans each
registered Module's class at registration time and builds a
``{CommandClass -> bound method}`` dispatch table.

There is no ``can_handle`` predicate. Routing is type-only. Conditional
handling is expressed by splitting the Command into narrower classes or
branching inside the handler body.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, TypeVar

if TYPE_CHECKING:
    from .host import ModuleHost


F = TypeVar("F", bound=Callable[..., object])


# Marker attribute name written onto decorated methods by ``@handles``.
# Read by ``ModuleHost.register`` when building the dispatch table.
HANDLES_ATTR = "__pymodules_handles__"


def handles(*commands: type) -> Callable[[F], F]:
    """
    Decorator marking a Module method as the handler for one or more Command classes.

    The decorator stores the claimed Command classes as a tuple on the
    function under ``__pymodules_handles__``. ``ModuleHost.register`` reads
    that marker to build the dispatch table.

    Most usages claim a single Command class. ``@handles(CmdA, CmdB)`` is
    permitted for the rare case of a method that handles multiple types.

    Example:
        class GreeterModule(Module):
            @handles(GreetCommand)
            def greet(self, command: GreetCommand) -> None:
                command.output = GreetOutput(message=f"Hello, {command.input.name}!")
                command.handled = True
    """
    if not commands:
        raise TypeError("@handles requires at least one Command class")

    def decorator(func: F) -> F:
        setattr(func, HANDLES_ATTR, tuple(commands))
        return func

    return decorator


@dataclass
class ModuleMetadata:
    """Metadata for a module."""

    name: str = ""
    description: str = ""
    version: str = "1.0.0"


def module(name: str = "", description: str = "", version: str = "1.0.0") -> Callable[[type], type]:
    """
    Decorator to add metadata to a module class.

    Example:
        @module(name="Greeter", description="Handles greeting commands")
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


class Module:
    """
    Base class for command handler modules.

    Subclass this and decorate methods with ``@handles(CommandClass)`` to
    claim Commands. Each module has access to its host via the ``host``
    property, allowing it to dispatch commands to other modules.

    Example:
        @module(name="Greeter", description="Handles greeting commands")
        class GreeterModule(Module):
            @handles(GreetCommand)
            def greet(self, command: GreetCommand) -> None:
                command.output = GreetOutput(
                    message=f"Hello, {command.input.name}!"
                )
                command.handled = True
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

    def on_load(self) -> None:
        """Called when the module is loaded into a host."""
        pass

    def on_unload(self) -> None:
        """Called when the module is unloaded from a host."""
        pass


__all__ = [
    "HANDLES_ATTR",
    "Module",
    "ModuleMetadata",
    "handles",
    "module",
]
