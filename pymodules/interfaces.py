"""
Core interfaces for the PyModules command-dispatch system.

Commands are typed in-process requests with a CommandRequest payload and a
CommandResponse. They flow through the ModuleHost and are dispatched to
exactly one claiming Module. The claiming Module **returns** its
CommandResponse; the Command itself is no longer mutated to carry the result.
"""

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


@dataclass
class CommandRequest:
    """
    Base class for a command's typed request payload.

    Subclass this to define the input parameters for your command.

    Example:
        @dataclass
        class GreetRequest(CommandRequest):
            name: str
    """

    pass


@dataclass
class CommandResponse:
    """
    Base class for a command's typed response.

    Subclass this to define the value returned by your command handler.

    Example:
        @dataclass
        class GreetResponse(CommandResponse):
            message: str
    """

    pass


@dataclass
class CommandContext:
    """
    Cross-cutting context carried alongside a Command.

    Holds the typed observability fields that middleware reads and writes
    (trace_id, correlation_id, parent_span_id) plus an ``extra`` dict as the
    escape hatch for genuinely ad-hoc keys that user code wants to pass
    through dispatch. Most keys should become typed fields here; ``extra``
    exists for cases that don't yet warrant a field of their own.

    Attributes:
        trace_id: Distributed-trace identifier; set by ``TracingMiddleware``
            from the current ``TraceContext`` (or by an inbound broker
            consumer copying the upstream header).
        correlation_id: End-to-end correlation identifier for logs and
            cross-service joins. Generated when no trace is active.
        parent_span_id: Span id of the caller that produced this dispatch.
            Used by exporters to stitch the in-process span into its parent.
        extra: Untyped escape hatch for ad-hoc keys (e.g., a span_id copied
            verbatim from a broker message header for round-trip
            preservation). Prefer adding a typed field if the key has a
            stable contract.
    """

    trace_id: str | None = None
    correlation_id: str | None = None
    parent_span_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Type variables for generic Command. ``Req`` is the request payload type
# carried into the handler; ``Resp`` is the response type the handler returns
# (propagated to ``ModuleHost.dispatch``'s return annotation).
Req = TypeVar("Req", bound=CommandRequest)
Resp = TypeVar("Resp", bound=CommandResponse)


@dataclass
class Command(Generic[Req, Resp]):
    """
    A command that can be dispatched through a ModuleHost.

    Commands carry a typed request into the claiming Module. The Module
    **returns** its typed response from the decorated handler; the Command
    is not mutated.

    Attributes:
        name: Unique identifier for this command type (e.g., "com.example.greet").
        request: Request data passed to the handler.
        context: Cross-cutting ``CommandContext`` (trace_id, correlation_id,
            parent_span_id, plus an ``extra`` dict). Replaces the old
            untyped ``meta: dict[str, Any]`` god-bag; observability
            middleware reads and writes typed fields rather than string
            keys.
        command_id: Optional caller-supplied identifier for idempotency.
            When set, ``IdempotencyMiddleware`` (if present) caches the
            response under this key and returns the cached value on a
            subsequent dispatch with the same id. ``None`` means the
            dispatch is not de-duplicated.

    The ``Resp`` type parameter exists for static typing only: it lets
    ``ModuleHost.dispatch(cmd: Command[Req, Resp]) -> Resp`` propagate the
    response type to the caller. There is no runtime ``response`` field.

    Example:
        @dataclass
        class GreetRequest(CommandRequest):
            name: str

        @dataclass
        class GreetResponse(CommandResponse):
            message: str

        class GreetCommand(Command[GreetRequest, GreetResponse]):
            name = "example.greet"
    """

    name: str = ""
    request: Req | None = None
    context: CommandContext = field(default_factory=CommandContext)
    command_id: str | None = None

    def __post_init__(self) -> None:
        # Allow subclasses to define name as class attribute
        if not self.name and hasattr(self.__class__, "name"):
            class_name = self.__class__.name
            if isinstance(class_name, str) and class_name:
                object.__setattr__(self, "name", class_name)


@dataclass
class Event:
    """
    Base class for an in-process broadcast notification.

    Unlike a ``Command``, an ``Event`` is fire-and-forget: it has no winning
    handler, no response value, and may be received by zero or more
    subscribers. Subclasses add their payload fields directly — there is no
    separate ``EventRequest``/``EventResponse`` typing because there is
    nothing to return.

    Events flow through an ``EventBus`` (in-process) rather than through the
    ``ModuleHost`` dispatch chain. ``Command`` and ``Event`` are deliberately
    separate primitives: ``Command`` = exactly one winner, returns a value,
    runs through the middleware chain. ``Event`` = N subscribers, no return,
    no middleware chain (errors are isolated per subscriber).

    Attributes:
        name: Logical event name, useful for logging/observability. May be
            set as a class attribute on the subclass.
        context: Cross-cutting ``CommandContext`` (trace_id, correlation_id,
            parent_span_id, plus an ``extra`` dict), mirroring
            ``Command.context``. Lets a publisher propagate the active
            trace into the events it emits so subscribers can stitch their
            spans onto the originating request.

    Example:
        @dataclass
        class UserCreated(Event):
            user_id: str = ""
            email: str = ""
            name: str = "user.created"
    """

    name: str = ""
    context: CommandContext = field(default_factory=CommandContext)

    def __post_init__(self) -> None:
        # Allow subclasses to define name as a class attribute, mirroring Command.
        if not self.name and hasattr(self.__class__, "name"):
            class_name = self.__class__.name
            if isinstance(class_name, str) and class_name:
                object.__setattr__(self, "name", class_name)
