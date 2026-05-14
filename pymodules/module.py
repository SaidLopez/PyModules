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
from typing import Any, ClassVar, TypeVar

from .interfaces import Command, CommandResponse, Event

# TypeVar bound to the response type of the Command class passed to ``@handles``.
# Used so that mypy can verify the decorated method's return type matches the
# declared Command's Resp parameter (approximately — see decorator docstring).
# Bound matches ``Command``'s own ``Resp`` bound from ``interfaces.py``.
Resp = TypeVar("Resp", bound=CommandResponse)
F = TypeVar("F", bound=Callable[..., Any])

# Marker attribute name written onto decorated methods by ``@handles``.
# Read by ``ModuleHost.register`` when building the dispatch table.
HANDLES_ATTR = "__pymodules_handles__"

# Marker attribute name written onto decorated methods by ``@subscribes``.
# Read by ``ModuleHost.register`` when wiring the EventBus.
SUBSCRIBES_ATTR = "__pymodules_subscribes__"

# Marker attribute name written onto ``@subscribes``-decorated methods when
# the decorator was called with ``route_by=...``. Read by
# :class:`pymodules.host.ModuleHost` at Agent-template registration time to
# decide whether each matching Event spawns a fresh AgentRun (no route_by)
# or routes to an existing AgentRun keyed by ``route_by(event)``
# (issue #14 / ADR-0008). Stored as a SEPARATE marker (rather than
# bundled into :data:`SUBSCRIBES_ATTR`) so the Module-side wiring path
# stays exactly the same shape it always was — the Agent path is the
# only one that consults the routing callable.
SUBSCRIBES_ROUTE_BY_ATTR = "__pymodules_subscribes_route_by__"


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


def subscribes(
    *events: "type[Event]",
    route_by: Callable[[Event], Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator marking a Module *or* Agent method as a subscriber for one or
    more Event classes.

    Sibling of ``@handles``. ``@handles`` claims a Command (one-winner,
    returns a response, runs through the middleware chain); ``@subscribes``
    listens for an Event (N-subscribers, no return, no middleware chain,
    errors are isolated per subscriber).

    The decorator stores the claimed Event classes as a tuple on the
    function under ``__pymodules_subscribes__``. ``ModuleHost.register``
    reads that marker and either:

    - For a Module: registers the bound method directly against the
      host-owned ``EventBus`` (the existing behaviour, unchanged).
    - For an Agent template (issue #14 / ADR-0008): registers a *wrapper*
      that spawns an :class:`~pymodules.agent.AgentRun` on each matching
      Event and invokes the decorated method on that fresh run.

    The optional ``route_by`` kwarg is **Agent-only** semantics. When
    supplied, it is stored under
    :data:`SUBSCRIBES_ROUTE_BY_ATTR` and read by the Agent-side wiring
    path: instead of spawning a fresh AgentRun per Event, the host calls
    ``route_by(event)`` to compute a routing key and looks for an
    existing :class:`AgentRun` of this template whose
    ``routing_key`` matches. If one is found, the decorated method is
    invoked on the live instance (preserving in-flight state). If none
    matches, a new AgentRun is spawned and the routing key is recorded
    on it for future events. ``route_by`` on a Module subscriber has no
    effect — the existing per-publish fan-out is type-only.

    The decorated method takes ``(self, event: EventClass)`` and returns
    nothing (or an awaitable of nothing). Both sync and async subscribers
    are supported.

    A Module may have any mix of ``@handles`` and ``@subscribes`` methods.
    Unlike ``@handles``, multiple Modules may subscribe to the same Event
    class — that is the whole point of pub/sub fan-out.

    Args:
        *events: One or more :class:`Event` subclasses this method handles.
            Routing is exact-type per ADR-0007 — a subscriber to a base
            Event class does not receive instances of derived classes.
        route_by: Optional callable, Agent-only. Receives the Event and
            returns the routing key (any value usable as a dict key /
            ``==``-comparable). Required ``None`` semantics: spawn a
            fresh AgentRun per matching Event (the default).

    Example::

        # Module (existing behaviour, no route_by):
        class AuditModule(Module):
            @subscribes(UserCreated)
            def log_user_created(self, event: UserCreated) -> None:
                self.audit_log.append((event.user_id, event.name))

        # Agent with route_by (new, issue #14):
        class TenantSaga(Agent):
            @subscribes(OrderPlaced, route_by=lambda e: e.tenant_id)
            def on_order(self, event: OrderPlaced) -> None:
                # All OrderPlaced events with the same tenant_id land on
                # the same in-flight AgentRun, sharing self state.
                ...
    """
    if not events:
        raise TypeError("@subscribes requires at least one Event class")

    for event_cls in events:
        if not isinstance(event_cls, type) or not issubclass(event_cls, Event):
            raise TypeError(
                f"@subscribes arguments must be Event subclasses; got {event_cls!r}"
            )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, SUBSCRIBES_ATTR, tuple(events))
        # ``route_by`` lives in its own marker so the Module-side path
        # never has to know about it: ``_collect_subscribers`` keeps its
        # existing ``(EventCls, bound_method)`` shape, and the new
        # ``_collect_agent_subscribers`` reads both markers to build
        # ``(EventCls, method_name, route_by)`` triples.
        if route_by is not None:
            setattr(func, SUBSCRIBES_ROUTE_BY_ATTR, route_by)
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
    **returns** the typed CommandResponse.

    A Module has **no reference back to its ``ModuleHost``**. Fan-out is
    expressed by publishing an ``Event`` to a broker (the Module holds the
    broker directly); cross-Module orchestration is the caller's job —
    dispatch one Command, inspect the response, dispatch the next. Calling
    back into the host from inside a handler would re-enter the middleware
    chain (re-charging rate-limit tokens, re-arming retries, hiding the
    call graph) and is intentionally not supported.

    Example:
        @module(name="Greeter", description="Handles greeting commands")
        class GreeterModule(Module):
            @handles(GreetCommand)
            def greet(self, command: GreetCommand) -> GreetResponse:
                return GreetResponse(message=f"Hello, {command.request.name}!")
    """

    _module_metadata: ModuleMetadata = ModuleMetadata()

    # Events this Module declares as published. Read by the (optional)
    # ``pymodules.contrib.fullstack`` AsyncAPI emitter and Outbound policy
    # registry to know which Event classes the Module commits to producing.
    # Default is the empty tuple, so existing Modules that don't declare
    # anything continue to work unchanged. Core does not consume this
    # attribute — it is a contract for the fullstack contrib.
    published_events: ClassVar[tuple[type[Event], ...]] = ()

    def __init__(self) -> None:
        # Initialize metadata from class if not set by decorator
        if not hasattr(self.__class__, "_module_metadata"):
            self.__class__._module_metadata = ModuleMetadata(name=self.__class__.__name__)

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
    "SUBSCRIBES_ATTR",
    "SUBSCRIBES_ROUTE_BY_ATTR",
    "Module",
    "ModuleMetadata",
    "handles",
    "module",
    "subscribes",
]
