"""
Agent + AgentRun — the active-producer primitive (sibling of Module).

Where a :class:`~pymodules.module.Module` is a *passive claimant* of a
:class:`~pymodules.interfaces.Command` (one method per command, routed to
exactly one Module by type), an :class:`Agent` is an *active producer*:
a class registered with a :class:`~pymodules.host.ModuleHost` as a
*template*; at runtime the host holds zero-to-many running
:class:`AgentRun` instances of that template. An ``AgentRun`` initiates
work — it dispatches Commands and publishes Events on its own schedule.

This module ships the foundation slice (ticket #10). Later tickets add
``@scheduled``, ``@subscribes`` routing inside Agents, the
``AgentStateStore`` Protocol, ``restart_policy`` / ``max_concurrent``,
and the ``AgentSpawner`` Protocol. The class is kept intentionally
minimal so those slices can extend it without rework.

Architectural references:

- ``CONTEXT.md`` — glossary entries "Agent" and "AgentRun".
- ``docs/adr/0008-agent-as-new-primitive.md`` — the design decision.
- ``docs/adr/0003-no-host-back-reference-on-modules.md`` — the rule
  ``Agent`` deliberately steps outside (see :class:`Agent` docstring).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar, runtime_checkable

from .exceptions import PyModulesError
from .interfaces import Event

if TYPE_CHECKING:
    from .agent_state import AgentStateStore
    from .host import ModuleHost
    from .resilience import RetryPolicy

# Marker attribute name written onto ``@scheduled``-decorated Agent
# methods by :func:`scheduled`. Read by
# :class:`pymodules.host.ModuleHost` at Agent-template registration time
# to discover which bound methods to feed into the lazy
# :class:`pymodules.scheduler.Scheduler`. Mirrors :data:`HANDLES_ATTR` /
# :data:`SUBSCRIBES_ATTR` in :mod:`pymodules.module`.
SCHEDULED_ATTR = "__pymodules_scheduled__"

F = TypeVar("F", bound=Callable[..., Any])


class AgentError(PyModulesError):
    """Base exception for Agent-related framework errors.

    Subclasses sit under this so callers (and ``except`` clauses in
    middleware/tests) can catch the whole family with one type.
    """

    pass


class AgentSpawnRejected(AgentError):
    """Raised by :meth:`ModuleHost.spawn` when ``max_concurrent`` is reached.

    A template that sets ``max_concurrent = N`` rejects the N+1th spawn
    with this exception rather than queueing — the caller decides whether
    to retry, drop, or alert (ADR-0008). After at least one of the
    in-flight AgentRuns for that template terminates, a subsequent
    ``spawn(...)`` that fits under the cap succeeds.
    """

    pass


class AgentRunStuck(AgentError):
    """Raised internally when an AgentRun ignores cooperative stop past the
    configured ``shutdown_grace`` and must be hard-cancelled.

    Carried as the ``error`` field on the terminal :class:`AgentFailed`
    Event the host publishes for that run, so subscribers can distinguish
    a stuck-run hard-cancel from a normal unhandled exception.
    """

    pass


@dataclass
class AgentFailed(Event):
    """Event published when an AgentRun terminates via unhandled exception
    or hard-cancel after stop-grace.

    Subscribers receive this via the existing ``@subscribes(AgentFailed)``
    machinery on Modules or Agents. Per ADR-0007, subscriber errors are
    isolated — one subscriber raising does not prevent the others from
    seeing the event.

    Attributes:
        agent_template_name: ``type(agent).__name__`` of the failed run,
            useful for routing alerts by template without holding the
            class object.
        agent_run_id: The AgentRun's UUIDv4 id (matches the key in
            :attr:`ModuleHost.agent_runs` while the run was alive).
        error: The exception that terminated the run. For hard-cancelled
            runs that ignored cooperative stop past ``shutdown_grace``,
            this is an :class:`AgentRunStuck`. For ``restart_policy``
            exhaustion, this is the LAST exception observed across
            attempts (no further restarts follow).
        name: Event name. Defaults to ``"agent.failed"``.
    """

    agent_template_name: str = ""
    agent_run_id: str = ""
    error: BaseException | None = None
    name: str = "agent.failed"


class AgentNotRegistered(AgentError):
    """Raised by :meth:`ModuleHost.spawn` when the requested template
    has not been registered with the host.

    Mirrors the spirit of :class:`UnknownCommandError` for the Agent
    side: the registry is the source of truth, and spawning an unknown
    template is a programmer error rather than a transient failure.

    Attributes:
        template: The :class:`Agent` subclass the caller tried to spawn.
    """

    def __init__(self, template: type[Agent]):
        super().__init__(
            f"No Agent template registered for {template.__name__}; "
            f"call host.register({template.__name__}()) before host.spawn(...)."
        )
        self.template = template


class Agent:
    """Base class for active-producer agents.

    Subclass this and optionally define an ``async def run(self) -> None``
    method: when an :class:`AgentRun` is spawned for the template, the
    host launches ``run()`` as a task. Returning from ``run()`` naturally
    terminates the AgentRun. The cooperative-stop pattern is to check
    ``self._run._stop_requested`` (or, in later slices, a public helper)
    at checkpoints inside ``run()`` and return when it goes true.

    **Host back-reference.** Unlike :class:`Module`, an ``Agent`` holds
    ``self._host`` — the deliberate exception to ADR-0003 documented in
    ``docs/adr/0008-agent-as-new-primitive.md``. The reason: ADR-0003
    bars re-entering the dispatch chain *from inside a chain frame*
    (which would double-charge rate-limit tokens, re-arm retries, and
    hide the call graph). An ``AgentRun`` runs on its own task — its
    dispatch is a fresh top-level entry into the chain, not a re-entry —
    so the accounting concerns do not apply. ``self._host.dispatch(...)``
    and ``self._host.publish(...)`` are therefore legitimate from inside
    an Agent; they flow through the host's existing middleware chain /
    EventBus exactly once, with no per-Agent chain.

    Later tickets will add class-level markers (``@scheduled``,
    ``@subscribes`` inside Agent classes) — for now the class is just
    enough to spawn, run, dispatch, and publish.

    Example::

        class GoalSeeker(Agent):
            async def run(self) -> None:
                while not self._run._stop_requested:
                    await self._host.dispatch_async(TickCommand())
                    await asyncio.sleep(1)

        host = ModuleHost()
        host.register(GoalSeeker())
        run = host.spawn(GoalSeeker)
        ...
        run.stop()
    """

    # Populated by the host immediately after the AgentRun is constructed,
    # before ``run()`` is scheduled. ``None`` on a freshly-instantiated
    # template that has not yet been spawned (e.g., the registered
    # template instance, or a unit test that constructs the Agent
    # standalone).
    _host: ModuleHost | None = None
    _run: AgentRun | None = None

    # Per-template override for the AgentStateStore an AgentRun of this
    # template uses (ADR-0008). When ``None`` — the default — the host
    # falls back to its own default store, installed without user opt-in.
    # Set this on a subclass to plug in a persistent backend for that
    # template only:
    #
    #     class Saga(Agent):
    #         state_store_factory = lambda: RedisAgentStateStore(url=...)
    #
    # The factory is called once per spawn, *not* once per template;
    # giving each AgentRun a fresh store instance is the caller's choice
    # if that is what they want.
    state_store_factory: ClassVar[Callable[[], AgentStateStore] | None] = None

    # Per-template concurrency cap (ADR-0008 / ticket #11). When set,
    # ``host.spawn(Template)`` raises :class:`AgentSpawnRejected` if the
    # number of in-flight AgentRuns of this template is already at the
    # cap. ``None`` (the default) disables the check — no upper bound.
    # No queueing: the caller decides whether to retry, drop, or alert.
    max_concurrent: ClassVar[int | None] = None

    # Per-template restart policy (ADR-0008 / ticket #11). When set, an
    # AgentRun that terminates via unhandled exception is re-spawned with
    # the same constructor kwargs up to ``policy.max_retries`` times,
    # honouring the policy's backoff between attempts. After exhaustion a
    # final :class:`AgentFailed` event is published and no further
    # restarts occur. Cooperative-stop exits never trigger a restart.
    # State is NOT restored across restarts — that is an explicit
    # out-of-scope per the ADR. ``None`` (the default) disables restart;
    # an unhandled exception terminates the run as it does today.
    #
    # Typed via a string forward-reference so this module stays free of
    # any import-time edge with ``pymodules.resilience``.
    restart_policy: ClassVar[RetryPolicy | None] = None


class AgentRun:
    """A single running instance of an :class:`Agent` template.

    Construction is *host-internal*. User code must obtain an
    ``AgentRun`` via :meth:`ModuleHost.spawn`; instantiating ``AgentRun``
    directly is supported only as a unit-testing seam and is not part of
    the public API surface (calling ``AgentRun(agent, host, store)`` from
    application code is unsupported and may break without notice).

    Attributes:
        id: Per-instance UUIDv4 string, generated at construction. Stable
            for the lifetime of the run and used as the key into
            :attr:`ModuleHost.agent_runs`.
        template: The :class:`Agent` subclass this run is an instance of.
            Kept as the class object (not the instance) because that is
            the identity callers reason about — the same template can
            produce many runs.
        state: Mutable per-run state dict. The Agent body reads/writes
            ``self._run.state`` freely; the dict is **not** persisted on
            every mutation. Durable snapshots happen only on explicit
            :meth:`checkpoint` calls and on AgentRun termination — see
            the ADR-0008 / ticket #12 contract pinned in the conformance
            test suite.

    The ``host`` back-reference is exposed as a read-only property so
    code patterns like ``run.host.dispatch_async(...)`` work as users
    would expect from the ADR-0008 spec, without leaking the underlying
    attribute to mutation.

    Stop semantics: :meth:`stop` sets the cooperative flag
    ``_stop_requested = True``. ``run()`` is expected to honour the flag
    at its next checkpoint and return. Hard-cancel and shutdown grace
    arrive in a follow-up ticket; this slice covers the cooperative
    path only (matching ADR-0008's v1 default).
    """

    def __init__(
        self,
        agent: Agent,
        host: ModuleHost,
        state_store: AgentStateStore | None = None,
        *,
        triggered_by_event: Event | None = None,
        routing_key: Any | None = None,
    ) -> None:
        # UUIDv4 — generated here so logs and `host.agent_runs` keys are
        # stable from the very first line of the run. ``str()`` so the
        # public surface is plain-string (matches ``Command.command_id``
        # and the rest of the framework).
        self.id: str = str(uuid.uuid4())
        self.template: type[Agent] = type(agent)
        self._agent: Agent = agent
        self._host: ModuleHost = host
        # Event-triggered spawn metadata (issue #14 / ADR-0008). Populated
        # by :meth:`ModuleHost.spawn` when an AgentRun is born from an
        # ``@subscribes``-decorated Agent method firing on an EventBus
        # publish. Both default to ``None`` so ``host.spawn(Template)``
        # called directly from user code (the manual-spawn path) leaves
        # them empty. They are documented as part of the public
        # observability surface — callers may inspect
        # ``run.triggered_by_event`` to see *what* Event spawned this run
        # and ``run.routing_key`` to see the value the
        # ``route_by=...`` callable returned for that Event.
        self.triggered_by_event: Event | None = triggered_by_event
        self.routing_key: Any | None = routing_key
        # Cooperative-stop flag. Read by ``run()`` (and any user
        # checkpoint code) via ``self._run._stop_requested`` from inside
        # the Agent body. The leading underscore signals "private to the
        # agent / host pair" — callers go through ``stop()``.
        self._stop_requested: bool = False
        # Per-run state dict. Initialised empty; the Agent body assigns
        # / mutates entries directly and persists them via ``checkpoint()``
        # or implicitly at termination. ADR-0008 forbids cross-AgentRun
        # state, so this dict is private to this run.
        self.state: dict[str, Any] = {}
        # Pluggable persistence behind ``checkpoint()``. ``None`` is a
        # legitimate value — it disables persistence entirely (useful in
        # unit tests that construct ``AgentRun`` standalone). The host
        # wires a real store on every spawn; callers who construct
        # ``AgentRun`` directly may pass one explicitly.
        self._state_store: AgentStateStore | None = state_store
        # Scheduled-method wiring (issue #13). If the host is a real
        # :class:`pymodules.host.ModuleHost` carrying a lazy scheduler
        # (constructed at template registration time when ``@scheduled``
        # methods were detected), hand it our bound methods so the
        # scheduler can start firing them. This is the integration seam
        # that lets the scheduler hook into spawn lifecycle *without*
        # modifying :meth:`ModuleHost.spawn`. Unit tests that construct
        # an :class:`AgentRun` with a stand-in host (no
        # ``_attach_scheduled_methods`` method) hit the AttributeError
        # branch and skip wiring, which is the correct behaviour for
        # those tests — they exercise ``AgentRun`` in isolation, not the
        # scheduler.
        attach = getattr(host, "_attach_scheduled_methods", None)
        if attach is not None:
            attach(self)

    @property
    def host(self) -> ModuleHost:
        """The :class:`ModuleHost` that spawned this run.

        Mirrors the design note in ADR-0008: an AgentRun has a
        back-reference to its host because it runs outside the dispatch
        chain frame, so the ADR-0003 re-entry concern does not apply.
        """
        return self._host

    @property
    def agent(self) -> Agent:
        """The bound :class:`Agent` instance for this run.

        Exposed so callers can reach the user-defined template body
        (e.g., to inspect per-run state in tests). The template type is
        already on :attr:`template`; this is the live instance.
        """
        return self._agent

    def stop(self) -> None:
        """Request cooperative termination of this run.

        Sets ``_stop_requested = True``. The ``run()`` coroutine is
        expected to check the flag at its next checkpoint and return.
        Idempotent: calling ``stop()`` repeatedly is safe — the flag
        simply stays true.

        This does not synchronously join the underlying task. Code that
        needs to wait for termination should observe disappearance from
        :attr:`ModuleHost.agent_runs`, or, for tighter integration,
        await the task directly via mechanisms a follow-up ticket will
        provide.
        """
        self._stop_requested = True

    def checkpoint(self) -> None:
        """Persist :attr:`state` to the configured :class:`AgentStateStore`.

        Explicit and synchronous: the Agent body decides *when* a
        snapshot becomes durable. The framework deliberately does not
        intercept attribute writes to make state durable on every
        mutation — that would be unpredictable in cost and would
        diverge from the durable-workflow-engine semantics ADR-0008
        adopts (Temporal, Cadence checkpoint on demand, not on every
        local variable change).

        A no-op when no store is wired (``self._state_store is None``).
        This keeps unit-test construction of ``AgentRun`` ergonomic — a
        test that builds an ``AgentRun`` directly without a store can
        still call ``checkpoint()`` without special-casing.
        """
        if self._state_store is None:
            return
        self._state_store.set(self.id, self.state)


@runtime_checkable
class AgentSpawner(Protocol):
    """Narrow capability handed to Modules for spawning Agents.

    Per ADR-0003 a :class:`~pymodules.module.Module` has no host
    back-reference. ``AgentSpawner`` is the *minimum* surface a Module
    needs to spawn an :class:`AgentRun` — exposing exactly one method
    and nothing else. The implementation in :class:`ModuleHost` is a
    thin adapter (``_BoundAgentSpawner``) so the runtime object cannot
    be widened by a cast: there is no ``dispatch`` / ``publish`` /
    ``register`` / ``agent_runs`` attribute to reach.

    Modules inject this in their ``__init__`` the same way they inject
    an :class:`~pymodules.eventbus.EventBus` today::

        class Saga(Module):
            def __init__(self, spawner: AgentSpawner) -> None:
                self._spawner = spawner

            @handles(StartSagaCommand)
            def on_start(self, cmd: StartSagaCommand) -> ...:
                run = self._spawner.spawn(SagaAgent, ...)
                ...

    The Protocol is ``runtime_checkable`` so ``isinstance(x, AgentSpawner)``
    works in tests; static type checkers see the same single-method
    surface and reject any other attribute access on a variable typed
    as ``AgentSpawner``.
    """

    def spawn(self, template: type[Agent], **kwargs: Any) -> AgentRun: ...


def scheduled(
    *,
    interval: timedelta | None = None,
    cron: str | None = None,
) -> Callable[[F], F]:
    """Decorator marking an Agent method to fire on a schedule (ADR-0008 / #13).

    Exactly one of ``interval=`` or ``cron=`` must be set. The decorator
    stores a :class:`pymodules.scheduler.Schedule` on the function under
    :data:`SCHEDULED_ATTR`; :class:`pymodules.host.ModuleHost` scans the
    Agent template's class at registration time and feeds the marked
    methods into a lazily-constructed
    :class:`pymodules.scheduler.Scheduler`.

    Usage::

        from datetime import timedelta
        from pymodules import Agent, scheduled

        class Watcher(Agent):
            @scheduled(interval=timedelta(seconds=30))
            async def poll(self) -> None:
                ...

            @scheduled(cron="0 * * * *")  # every hour on the hour
            def hourly_report(self) -> None:
                ...

    Re-entry: if the previous invocation is still running at the next
    fire time, the scheduler **skips** that tick and logs a warning
    (see :mod:`pymodules.scheduler` for the rationale).

    Args:
        interval: Fire every ``timedelta`` of clock-time. Must be > 0.
        cron: Standard 5-field cron expression (minute hour dom month
            dow). The parser supports ``*``, integers, ``a-b`` ranges,
            ``a,b,c`` lists, and ``*/step`` step values. Day-of-week
            uses 0-6 with Monday=0 (Python ``date.weekday()``).

    Raises:
        TypeError: at decoration time, if neither or both of
            ``interval`` / ``cron`` are set.
        ValueError: at decoration time, if ``cron`` is syntactically
            invalid or ``interval`` is non-positive.
    """
    # Import here to keep ``pymodules.agent`` free of any eager
    # dependency on ``pymodules.scheduler``: a user who never touches
    # ``@scheduled`` does not pull the scheduler module into memory.
    from .scheduler import Schedule

    # Eager validation: build the Schedule now so a malformed cron
    # expression or zero interval fails at decoration time, not at
    # first fire.
    schedule = Schedule(interval=interval, cron=cron)

    def decorator(func: F) -> F:
        setattr(func, SCHEDULED_ATTR, schedule)
        return func

    return decorator


__all__ = [
    "Agent",
    "AgentRun",
    "AgentError",
    "AgentFailed",
    "AgentNotRegistered",
    "AgentRunStuck",
    "AgentSpawnRejected",
    "AgentSpawner",
    "scheduled",
]
