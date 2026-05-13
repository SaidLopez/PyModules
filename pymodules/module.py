"""
Module base class for PyModules.

Modules are command handlers whose methods are individually decorated with
``@handles(CommandClass)`` to claim Commands. ``ModuleHost`` scans each
registered Module's class at registration time and builds a
``{CommandClass -> bound method}`` dispatch table.

There is no ``can_handle`` predicate. Routing is type-only. Conditional
handling is expressed by splitting the Command into narrower classes or
branching inside the handler body.

Handlers **return** their typed CommandResponse. The host's
``dispatch(cmd: Command[Req, Resp]) -> Resp`` returns that value.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, TypeVar

from .interfaces import Command, CommandResponse

if TYPE_CHECKING:
    from .host import ModuleHost


# TypeVar bound to the response type of the Command class passed to ``@handles``.
# Used so that mypy can verify the decorated method's return type matches the
# declared Command's Resp parameter (approximately — see decorator docstring).
# Bound matches ``Command``'s own ``Resp`` bound from ``interfaces.py``.
Resp = TypeVar("Resp", bound=CommandResponse)
F = TypeVar("F", bound=Callable[..., Any])


# Marker attribute name written onto decorated methods by ``@handles``.
# Read by ``ModuleHost.register`` when building the dispatch table.
HANDLES_ATTR = "__pymodules_handles__"


def handles(
    *commands: "type[Command[Any, Resp]]",
) -> Callable[[Callable[..., Resp]], Callable[..., Resp]]:
    """
    Decorator marking a Module method as the handler for one or more Command classes.

    The decorator stores the claimed Command classes as a tuple on the
    function under ``__pymodules_handles__``. ``ModuleHost.register`` reads
    that marker to build the dispatch table.

    The decorated method takes ``(self, command: C)`` and **returns** the
    response value. ``ModuleHost.dispatch`` propagates that return value
    as the dispatch result.

    Most usages claim a single Command class. ``@handles(CmdA, CmdB)`` is
    permitted for the rare case of a method that handles multiple types;
    in that case the response type is the common supertype of the claimed
    Commands' ``Resp`` parameters (often ``CommandResponse``).

    Typing note: the decorator's ``Resp`` TypeVar binds against the
    ``Command[Any, Resp]`` argument(s) and flows to the decorated method's
    return type. Multi-Command claims with heterogeneous response types
    will resolve ``Resp`` to a common supertype rather than rejecting the
    declaration — mypy does not enforce a strict per-Command response
    match across a tuple of Command classes.

    Example:
        class GreeterModule(Module):
            @handles(GreetCommand)
            def greet(self, command: GreetCommand) -> GreetResponse:
                return GreetResponse(message=f"Hello, {command.request.name}!")
    """
    if not commands:
        raise TypeError("@handles requires at least one Command class")

    def decorator(func: Callable[..., Resp]) -> Callable[..., Resp]:
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
    claim Commands. Each decorated method takes the typed Command and
    **returns** the typed CommandResponse. Each module has access to its
    host via the ``host`` property, allowing it to dispatch commands to
    other modules.

    Example:
        @module(name="Greeter", description="Handles greeting commands")
        class GreeterModule(Module):
            @handles(GreetCommand)
            def greet(self, command: GreetCommand) -> GreetResponse:
                return GreetResponse(message=f"Hello, {command.request.name}!")
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
