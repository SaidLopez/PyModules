"""
ModuleHost - Central dispatcher for the PyModules command system.

Dispatch is a middleware chain. ``ModuleHost`` composes the configured
middleware (from ``ModuleHostConfig.middleware``) plus a built-in terminal
middleware once at construction. ``dispatch_async()`` invokes the chain;
``dispatch()`` is a thin sync wrapper that refuses to bridge:

- If the resolved handler is a coroutine function, sync ``dispatch()``
  raises ``SyncDispatchOnAsyncHandlerError``.
- If a loop is already running in the calling thread, sync ``dispatch()``
  raises ``SyncDispatchInAsyncContextError``.

The terminal middleware looks up ``type(command)`` in the dispatch table.
Sync handlers are bridged to async via the host's ``ThreadPoolExecutor``.
Unmatched dispatches raise ``UnknownCommandError`` (a ``PyModulesSignal``);
middleware that wants to observe rather than swallow it should catch and
re-raise.
"""

import asyncio
import inspect
import time
import types
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar, overload

from .agent import (
    SCHEDULED_ATTR,
    Agent,
    AgentFailed,
    AgentNotRegistered,
    AgentRun,
    AgentRunStuck,
    AgentSpawner,
    AgentSpawnRejected,
)
from .agent_state import AgentStateStore, InMemoryAgentStateStore
from .config import ModuleHostConfig
from .eventbus import EventBus
from .exceptions import (
    CommandHandlingError,
    DuplicateCommandError,
    ModuleRegistrationError,
    PyModulesSignal,
    SyncDispatchInAsyncContextError,
    SyncDispatchOnAsyncHandlerError,
    UnknownCommandError,
)
from .interfaces import Command, CommandRequest, CommandResponse, Event
from .logging import configure_logging, host_logger
from .middleware import Middleware, NextCall
from .module import HANDLES_ATTR, SUBSCRIBES_ATTR, SUBSCRIBES_ROUTE_BY_ATTR, Module

# Marker attribute written by
# ``pymodules.contrib.fullstack.outbound_policy.outbound_policy``. Read at
# Module-registration time to wire the bound method into the host's
# lazily-constructed ``OutboundPolicyRegistry``. The string is duplicated
# here (rather than imported from the contrib package) so core stays free
# of any ``pymodules.contrib.fullstack`` import — the contrib decorator
# owns the canonical constant; this is a deliberate near-duplicate.
_OUTBOUND_POLICY_ATTR = "__pymodules_outbound_policy__"

# TypeVars for typed dispatch surface. ``Req``/``Resp`` propagate from the
# Command's generic parameters through ``dispatch`` to the caller.
Req = TypeVar("Req", bound=CommandRequest)
Resp = TypeVar("Resp", bound=CommandResponse)


class _BoundAgentSpawner:
    """Thin adapter exposing exactly :meth:`spawn` on a bound host.

    The runtime implementation of the :class:`AgentSpawner` Protocol
    handed out by :attr:`ModuleHost.agent_spawner`. Deliberately a
    purpose-built class with ``__slots__`` and a single public method —
    not a closure, not a ``functools.partial``, not a ``types.SimpleNamespace``
    — so a Module that holds an ``AgentSpawner`` cannot widen it back to
    the full ``ModuleHost`` surface via attribute access, ``__getattr__``,
    or stash inspection. This preserves the ADR-0003 invariant
    (Modules hold no host back-reference) even though the adapter itself
    is host-bound internally.

    Construction is host-internal: user code obtains an instance via
    :attr:`ModuleHost.agent_spawner` (cached, lazily constructed).
    """

    __slots__ = ("_host",)

    def __init__(self, host: "ModuleHost") -> None:
        self._host = host

    def spawn(self, template: type[Agent], **kwargs: Any) -> AgentRun:
        """Spawn an :class:`AgentRun` of ``template`` via the bound host.

        Delegates to :meth:`ModuleHost.spawn` unchanged; this adapter
        narrows the *type* a Module sees, not the runtime semantics.
        """
        return self._host.spawn(template, **kwargs)


class ModuleHost:
    """
    Central coordinator that manages modules and dispatches commands.

    The middleware chain is composed once at construction from
    ``config.middleware + [terminal]``. ``dispatch_async`` runs the chain;
    ``dispatch`` is a thin sync wrapper that does not bridge async.

    Example:
        from pymodules.resilience import default_middleware

        config = ModuleHostConfig(
            middleware=default_middleware(rate_limit=100, retry_max=3),
        )
        host = ModuleHost(config=config)
        host.register(GreeterModule())

        response = host.dispatch(GreetCommand(request=GreetRequest(name="World")))
    """

    def __init__(self, config: ModuleHostConfig | None = None):
        self._config = config or ModuleHostConfig.from_env()
        self._modules: list[Module] = []
        # type-routed dispatch table: Command class -> bound handler method
        self._dispatch_table: dict[type, Callable[[Command[Any, Any]], Any]] = {}
        # In-process EventBus owned by the host. Same lifetime as the host;
        # ``@subscribes`` methods on registered Modules are auto-wired here.
        # ``ModuleHost`` does not publish on the Module's behalf — Modules
        # call ``host.publish(SomeEvent(...))`` (or hold a reference to the
        # bus directly) when they want fan-out.
        self._event_bus = EventBus()
        # Agent registries (ADR-0008). ``_agent_templates`` keyed by the
        # Agent subclass so spawn(Template) is an O(1) lookup; values are
        # the registered template *instances*. ``_agent_runs`` is the
        # in-flight set keyed by the AgentRun's UUID. We expose a
        # read-only ``MappingProxyType`` view via ``agent_runs``.
        self._agent_templates: dict[type[Agent], Agent] = {}
        self._agent_runs: dict[str, AgentRun] = {}
        # Per-run task handles so we can clean up the dict on natural
        # termination. Not part of the public surface; later tickets will
        # extend this for hard-cancel / shutdown grace.
        self._agent_tasks: dict[str, asyncio.Task[Any]] = {}
        # Default :class:`AgentStateStore` (ADR-0008 / ticket #12).
        # Installed without user opt-in so every spawn has *some* place
        # to persist state. AgentRuns whose template defines a non-None
        # ``state_store_factory`` call that factory at spawn-time and
        # use the result instead of this default. A future config knob
        # may surface this as ``ModuleHostConfig.default_state_store``;
        # for now the host owns the lifetime of a single in-memory store
        # shared across spawns of all templates that did not override.
        self._default_state_store: AgentStateStore = InMemoryAgentStateStore()
        # Cached :class:`_BoundAgentSpawner` for this host. Constructed
        # lazily on first access to :attr:`agent_spawner` — hosts whose
        # registered Modules never spawn Agents never instantiate the
        # adapter. Cached after first access so a Module that holds the
        # spawner across many handler invocations always sees the same
        # identity (useful for ``isinstance`` / equality assertions in
        # tests).
        self._agent_spawner: _BoundAgentSpawner | None = None
        # Outbound policy registry (issue #4 / ADR-0009). Lazily
        # constructed by ``outbound_policies`` the first time a Module
        # with ``published_events`` or an ``@outbound_policy``-decorated
        # method registers — hosts that touch neither never instantiate
        # the registry. The type is intentionally ``Any`` here so
        # ``pymodules.host`` keeps zero static dependency on the
        # ``pymodules.contrib.fullstack`` package (per ADR-0002).
        self._outbound_policies: Any = None
        # Lazy :class:`pymodules.scheduler.Scheduler` (issue #13 /
        # ADR-0008). ``None`` until an Agent template carrying at least
        # one ``@scheduled`` method is registered — hosts whose
        # registered Agents only define ``async def run()`` (or no
        # callable at all) never construct a scheduler. Type is ``Any``
        # to keep this module free of an eager import of
        # :mod:`pymodules.scheduler` at class-construction time; the
        # actual import happens inside :meth:`_register_agent`.
        self._scheduler: Any = None
        # Per-template scheduled-method declarations, accumulated at
        # registration time. Keyed by the Agent class so a fresh spawn
        # can re-scan the live instance's bound methods without
        # re-walking the class dict. The value is a list of
        # ``(method_name, Schedule)`` pairs — the method itself is
        # bound at spawn time, since the registered template instance
        # is a prototype, not the live runner.
        self._agent_scheduled_decls: dict[type[Agent], list[tuple[str, Any]]] = {}
        self._executor = ThreadPoolExecutor(max_workers=self._config.max_workers)
        self._start_time = time.time()

        # Compose the chain once: configured middleware (outermost first),
        # then the host's terminal middleware.
        self._chain = self._build_chain(list(self._config.middleware))

        configure_logging(level=self._config.log_level)
        host_logger.debug(
            "ModuleHost initialized with max_workers=%d, propagate_exceptions=%s, "
            "middleware_count=%d",
            self._config.max_workers,
            self._config.propagate_exceptions,
            len(self._config.middleware),
        )

    @property
    def config(self) -> ModuleHostConfig:
        """Current configuration."""
        return self._config

    @property
    def modules(self) -> list[Module]:
        """List of registered modules."""
        return self._modules.copy()

    @property
    def event_bus(self) -> EventBus:
        """
        The in-process ``EventBus`` owned by this host.

        ``@subscribes``-decorated methods on registered Modules are
        automatically wired here at registration time. Code that wants to
        subscribe a free function or to publish events directly can use
        ``host.event_bus.subscribe(...)`` / ``host.publish(...)``.
        """
        return self._event_bus

    @property
    def outbound_policies(self) -> Any:
        """The host's :class:`OutboundPolicyRegistry`, lazily constructed.

        Owned by ``pymodules.contrib.fullstack`` (issue #4 / ADR-0009).
        The first access — whether by user code, by the SSE slice, or by
        the ``@outbound_policy`` scan in :meth:`register` — materialises
        a fresh registry; subsequent accesses return the same instance.

        Hosts whose registered Modules declare neither ``published_events``
        nor an ``@outbound_policy``-decorated method never touch this
        property, and therefore never import the contrib package — core
        stays standalone per ADR-0002.
        """
        if self._outbound_policies is None:
            # Import lazily so core never pulls the contrib package on
            # plain ``import pymodules``.
            from .contrib.fullstack.outbound_policy import OutboundPolicyRegistry

            self._outbound_policies = OutboundPolicyRegistry()
        return self._outbound_policies

    @property
    def uptime_seconds(self) -> float:
        """Time since host was created, in seconds."""
        return time.time() - self._start_time

    @property
    def agent_runs(self) -> Mapping[str, AgentRun]:
        """Read-only view of in-flight :class:`AgentRun` instances, keyed by id.

        Returns a ``types.MappingProxyType`` over the internal dict, so
        callers can iterate / look up by id but cannot mutate the
        registry. AgentRuns appear here on ``host.spawn(...)`` and
        disappear on natural termination (``run()`` returning), on
        ``run.stop()`` cooperatively honoured by ``run()``, or — in a
        follow-up ticket — on host shutdown.
        """
        return types.MappingProxyType(self._agent_runs)

    @property
    def agent_spawner(self) -> AgentSpawner:
        """Narrow :class:`AgentSpawner` handle for this host (issue #15).

        Returns a cached :class:`_BoundAgentSpawner` typed as the
        :class:`AgentSpawner` Protocol — exactly one method, ``spawn``,
        and nothing else. Modules that need to spawn Agents from a
        Command handler inject *this* (not the host) in their
        constructor, preserving the ADR-0003 "no host back-reference on
        Modules" invariant.

        The adapter is constructed lazily on first access; subsequent
        accesses return the same instance, so callers can compare by
        identity (``host.agent_spawner is host.agent_spawner``).
        """
        if self._agent_spawner is None:
            self._agent_spawner = _BoundAgentSpawner(self)
        return self._agent_spawner

    @property
    def scheduler(self) -> Any:
        """The host's lazy :class:`pymodules.scheduler.Scheduler`, or ``None``.

        ``None`` until at least one Agent template carrying a
        ``@scheduled``-decorated method has been registered. After that,
        the same :class:`~pymodules.scheduler.Scheduler` instance is
        returned for the lifetime of the host. Lazy construction means
        a host whose Agents only define ``async def run()`` (or no
        callable at all) never instantiates a scheduler — the import of
        :mod:`pymodules.scheduler` is also deferred to the first
        registration.

        Typed as ``Any`` here so :mod:`pymodules.host` keeps zero
        static dependency on :mod:`pymodules.scheduler` at module-load
        time, matching the same pattern used for
        :attr:`outbound_policies`.
        """
        return self._scheduler

    # ------------------------------------------------------------------
    # Middleware chain composition
    # ------------------------------------------------------------------

    async def _terminal(self, command: Command[Any, Any]) -> Any:
        """
        Built-in terminal of the middleware chain.

        Looks up ``type(command)`` in the dispatch table and invokes the
        claiming handler. Sync handlers run on the host's executor.
        Raises ``UnknownCommandError`` if no Module claims ``type(command)``.

        Signature note: the terminal does not take a ``next`` parameter —
        it is the end of the chain. The chain builder adapts it.
        """
        handler = self._dispatch_table.get(type(command))
        if handler is None:
            raise UnknownCommandError(type(command))

        module = handler.__self__  # type: ignore[attr-defined]
        host_logger.debug(
            "Module %s handling command %s",
            module.metadata.name,
            command.name,
        )
        # Terminal raises raw exceptions; middleware (retry, DLQ,
        # fallback, ...) sees them directly. The host's outermost wrapper
        # is responsible for honouring ``propagate_exceptions``.
        if inspect.iscoroutinefunction(handler):
            return await handler(command)
        # Sync handler: run on the executor so we don't block the loop.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, handler, command)

    def _build_chain(self, middlewares: list[Middleware]) -> NextCall:
        """
        Compose ``middlewares + [terminal]`` into a single async callable.

        The first middleware is the outermost wrapper. Composition is
        right-fold: the terminal becomes the innermost ``next``.
        """
        next_call: NextCall = self._terminal

        # We need each wrapped layer to call the layer it was registered
        # to wrap, not the latest ``next_call``. Build from the back.
        for mw in reversed(middlewares):
            inner = next_call

            async def call(
                command: Command[Any, Any], _mw: Middleware = mw, _inner: NextCall = inner
            ) -> Any:
                return await _mw(command, _inner)

            next_call = call

        return next_call

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _collect_handlers(
        self, module: Module
    ) -> list[tuple[type, Callable[[Command[Any, Any]], Any]]]:
        """Scan ``module``'s class for ``@handles``-decorated methods."""
        pairs: list[tuple[type, Callable[[Command[Any, Any]], Any]]] = []
        for name, member in inspect.getmembers(type(module), predicate=inspect.isfunction):
            claims = getattr(member, HANDLES_ATTR, None)
            if not claims:
                continue
            bound = getattr(module, name)
            for cmd_class in claims:
                pairs.append((cmd_class, bound))
        return pairs

    def _collect_subscribers(
        self, module: Module
    ) -> list[tuple[type[Event], Callable[[Event], Any]]]:
        """Scan ``module``'s class for ``@subscribes``-decorated methods."""
        pairs: list[tuple[type[Event], Callable[[Event], Any]]] = []
        for name, member in inspect.getmembers(type(module), predicate=inspect.isfunction):
            claims = getattr(member, SUBSCRIBES_ATTR, None)
            if not claims:
                continue
            bound = getattr(module, name)
            for event_class in claims:
                pairs.append((event_class, bound))
        return pairs

    def _collect_agent_subscribers(
        self, template: Agent
    ) -> list[tuple[type[Event], str, Callable[[Event], Any] | None]]:
        """Scan an Agent template's class for ``@subscribes``-decorated methods.

        Mirrors :meth:`_collect_subscribers` but for the Agent side
        (issue #14 / ADR-0008). Returns
        ``(EventCls, method_name, route_by)`` triples — the bound method
        is resolved at spawn time, since an Agent's live instance is a
        fresh per-spawn object (the registered template instance is a
        prototype). ``route_by`` is ``None`` when the decorator was used
        without that kwarg (the spawn-new-per-Event default).

        Critically, Agent ``@subscribes`` methods do **not** go through
        the same direct ``event_bus.subscribe(EventCls, bound_method)``
        wiring that Modules use: an Event for an Agent fires the
        spawn-or-route wrapper installed by :meth:`_register_agent`,
        not the bound method directly.
        """
        triples: list[tuple[type[Event], str, Callable[[Event], Any] | None]] = []
        for name, member in inspect.getmembers(
            type(template), predicate=inspect.isfunction
        ):
            claims = getattr(member, SUBSCRIBES_ATTR, None)
            if not claims:
                continue
            route_by = getattr(member, SUBSCRIBES_ROUTE_BY_ATTR, None)
            for event_class in claims:
                triples.append((event_class, name, route_by))
        return triples

    def _collect_scheduled(
        self, template: Agent
    ) -> list[tuple[str, Any]]:
        """Scan an Agent template's class for ``@scheduled``-decorated methods.

        Mirrors :meth:`_collect_handlers` / :meth:`_collect_subscribers`
        but for the Agent side. Returns ``(method_name, Schedule)`` pairs
        — the bound method is resolved at spawn time, since the registered
        template instance is a prototype and the live runner is a fresh
        instance per spawn.
        """
        pairs: list[tuple[str, Any]] = []
        for name, member in inspect.getmembers(
            type(template), predicate=inspect.isfunction
        ):
            schedule = getattr(member, SCHEDULED_ATTR, None)
            if schedule is None:
                continue
            pairs.append((name, schedule))
        return pairs

    def _collect_outbound_policies(
        self, module: Module
    ) -> list[tuple[type[Event], Callable[..., bool]]]:
        """Scan ``module``'s class for ``@outbound_policy``-decorated methods.

        Mirrors :meth:`_collect_subscribers`. The decorator
        (:func:`pymodules.contrib.fullstack.outbound_policy.outbound_policy`)
        stores the claimed Event class on the function as
        ``_OUTBOUND_POLICY_ATTR``; we return ``(EventCls, bound_method)``
        pairs ready to feed into ``host.outbound_policies.register(...)``.
        """
        pairs: list[tuple[type[Event], Callable[..., bool]]] = []
        for name, member in inspect.getmembers(type(module), predicate=inspect.isfunction):
            claim = getattr(member, _OUTBOUND_POLICY_ATTR, None)
            if claim is None:
                continue
            bound = getattr(module, name)
            pairs.append((claim, bound))
        return pairs

    @overload
    def register(self, module: Module, override: bool = False) -> "ModuleHost": ...

    @overload
    def register(self, module: Agent, override: bool = False) -> "ModuleHost": ...

    def register(
        self, module: Module | Agent, override: bool = False
    ) -> "ModuleHost":
        """Register a :class:`Module` *or* :class:`Agent` instance.

        For a :class:`Module` (the original path, unchanged): scans the
        class for ``@handles`` / ``@subscribes`` decorators, wires the
        dispatch table and EventBus subscriptions, calls
        ``module.on_load()``, and rolls back on failure.

        For an :class:`Agent` (ADR-0008): stores the *template instance*
        keyed by its class in the agent-template registry, replacing any
        previous registration for the same class. Later tickets (#11–#15)
        will scan the Agent class here for ``@scheduled`` / ``@subscribes``
        markers; this slice keeps the path minimal — register-then-spawn.

        Returns ``self`` so registrations can be chained.
        """
        if isinstance(module, Agent):
            return self._register_agent(module)
        try:
            new_pairs = self._collect_handlers(module)
            new_subscriptions = self._collect_subscribers(module)
            new_outbound_policies = self._collect_outbound_policies(module)

            if not override:
                for cmd_class, _ in new_pairs:
                    existing = self._dispatch_table.get(cmd_class)
                    if existing is not None and existing.__self__ is not module:  # type: ignore[attr-defined]
                        existing_module = existing.__self__  # type: ignore[attr-defined]
                        raise DuplicateCommandError(
                            f"Command {cmd_class.__name__} is already claimed by "
                            f"{type(existing_module).__name__} "
                            f"(module name: {existing_module.metadata.name}); "
                            f"{type(module).__name__} "
                            f"(module name: {module.metadata.name}) cannot also claim it. "
                            "Pass override=True to ModuleHost.register to replace."
                        )

            self._modules.append(module)

            for cmd_class, bound in new_pairs:
                self._dispatch_table[cmd_class] = bound

            for event_class, bound_sub in new_subscriptions:
                # Multiple Modules may subscribe to the same Event class —
                # that is the whole point of pub/sub fan-out. No override
                # / collision check here.
                self._event_bus.subscribe(event_class, bound_sub)

            # Outbound policy wiring (issue #4 / ADR-0009). Triggers
            # lazy construction of ``outbound_policies`` if and only if
            # this Module declares either ``published_events`` or any
            # ``@outbound_policy``-decorated method — silent Modules
            # leave the registry uninstantiated.
            if new_outbound_policies or getattr(
                type(module), "published_events", ()
            ):
                registry = self.outbound_policies
                for event_class, bound_policy in new_outbound_policies:
                    # ``override=False`` — double-registration of an
                    # outbound filter is a loud failure (cross-tenant
                    # leakage footgun); the rollback handler below
                    # cleans up partial state.
                    registry.register(event_class, bound_policy)

            module.on_load()

            host_logger.info(
                "Registered module: %s (v%s) claiming %d command type(s), "
                "%d event subscription(s), %d outbound policy(ies)",
                module.metadata.name,
                module.metadata.version,
                len(new_pairs),
                len(new_subscriptions),
                len(new_outbound_policies),
            )
        except DuplicateCommandError:
            if module in self._modules:
                self._modules.remove(module)
            raise
        except Exception as e:
            # ``OutboundPolicyConflict`` (contrib-fullstack, issue #4)
            # gets the same loud-and-raw treatment as
            # ``DuplicateCommandError``: cross-tenant outbound conflicts
            # are framework-level signals the caller must see verbatim,
            # not wrapped in ``ModuleRegistrationError``. We resolve the
            # type lazily so core stays free of any
            # ``pymodules.contrib.fullstack`` import at module load
            # (ADR-0002).
            try:
                from .contrib.fullstack.exceptions import (
                    OutboundPolicyConflict as _OutboundPolicyConflict,
                )
            except ImportError:  # pragma: no cover — fullstack always ships
                _OutboundPolicyConflict = ()  # type: ignore[assignment,misc]
            if module in self._modules:
                self._modules.remove(module)
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            self._unsubscribe_module(module)
            self._remove_outbound_policies(module)
            if isinstance(e, _OutboundPolicyConflict):
                raise
            host_logger.error("Failed to register module %s: %s", type(module).__name__, e)
            raise ModuleRegistrationError(f"Failed to register {type(module).__name__}: {e}") from e

        return self

    def _unsubscribe_module(self, module: Module) -> None:
        """Remove every EventBus subscription whose handler is bound to ``module``."""
        for event_class, bound in self._collect_subscribers(module):
            self._event_bus.unsubscribe(event_class, bound)

    def _remove_outbound_policies(self, module: Module) -> None:
        """Remove every outbound policy whose callable is bound to ``module``.

        Called from the rollback handler in :meth:`register` and from
        :meth:`unregister` to keep the registry in lock-step with the
        live module list. No-op if the lazy registry was never
        constructed.
        """
        if self._outbound_policies is None:
            return
        registry = self._outbound_policies
        # Reach into the private dict deliberately — the registry's
        # public surface is intentionally tiny (register / has_policy /
        # apply); registration *removal* belongs to the host that owns
        # the registry's lifecycle, not to user code.
        for event_class, bound in self._collect_outbound_policies(module):
            existing = registry._policies.get(event_class)
            # ``existing == bound`` rather than ``is`` — each
            # ``getattr(module, name)`` call creates a fresh
            # ``MethodType`` wrapper, so identity would mismatch even
            # though the underlying ``(__self__, __func__)`` pair is
            # the same. ``MethodType.__eq__`` compares both.
            if existing == bound:
                del registry._policies[event_class]

    def unregister(self, module: Module) -> "ModuleHost":
        """Unregister a module and remove its dispatch + subscription entries."""
        if module in self._modules:
            try:
                module.on_unload()
            except Exception as e:
                host_logger.warning(
                    "Error during on_unload for %s: %s",
                    module.metadata.name,
                    e,
                )
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            self._unsubscribe_module(module)
            self._remove_outbound_policies(module)
            self._modules.remove(module)
            host_logger.info("Unregistered module: %s", module.metadata.name)
        return self

    def can_handle(self, command: Command[Any, Any]) -> bool:
        """True if a registered Module claims ``type(command)``."""
        return type(command) in self._dispatch_table

    # ------------------------------------------------------------------
    # Agent registration + spawn (ADR-0008)
    # ------------------------------------------------------------------

    def _register_agent(self, agent: Agent) -> "ModuleHost":
        """Store ``agent`` as the registered template for its class.

        Templates are keyed by ``type(agent)``: registering a second
        instance of the same class deliberately replaces the first, the
        same way two ``host.register(SomeModule(), override=True)``
        calls behave.

        Scanning (issue #13): the class is walked for
        ``@scheduled``-decorated methods. If any are found, a
        :class:`pymodules.scheduler.Scheduler` is constructed (the first
        such registration is what materialises the scheduler — hosts
        with no scheduled Agents never instantiate one) and the
        ``(method_name, Schedule)`` declarations are stashed on the
        host so each spawn of this template can wire its fresh
        instance's bound methods into the scheduler.
        """
        self._agent_templates[type(agent)] = agent
        scheduled_decls = self._collect_scheduled(agent)
        if scheduled_decls:
            self._agent_scheduled_decls[type(agent)] = scheduled_decls
            if self._scheduler is None:
                # Import lazily so a host with no scheduled Agents never
                # pulls :mod:`pymodules.scheduler` into memory. Mirrors
                # the lazy-import pattern used for
                # :attr:`outbound_policies`.
                from .scheduler import Scheduler

                self._scheduler = Scheduler()
                # Install the alive-predicate so each scheduled-method
                # loop self-terminates the moment its AgentRun is gone
                # from :attr:`agent_runs` (natural ``run()`` return,
                # cooperative ``run.stop()``, host shutdown). This is
                # the integration seam that lets the scheduler honour
                # :meth:`shutdown` without :meth:`shutdown` having to
                # touch the scheduler directly.
                self._scheduler.set_run_alive_predicate(
                    lambda run_id: run_id in self._agent_runs
                )

        # Wire ``@subscribes``-decorated methods on the Agent template
        # (issue #14 / ADR-0008). For each ``(EventCls, method_name,
        # route_by)`` triple we install a wrapper on the host's EventBus
        # — Agents do NOT go through the same direct
        # ``event_bus.subscribe(EventCls, bound_method)`` path Modules
        # take, because the bound method needs to fire on a *live*
        # AgentRun's instance, not on the registered template prototype.
        # The wrapper handles spawn-new (default) vs routing-by-key
        # (when ``route_by`` is supplied) and isolates
        # :class:`AgentSpawnRejected` so a cap-hit on one Agent does not
        # propagate to other subscribers of the same Event (ADR-0007).
        agent_subscribers = self._collect_agent_subscribers(agent)
        template_class = type(agent)
        for event_class, method_name, route_by in agent_subscribers:
            wrapper = self._make_agent_subscriber_wrapper(
                template_class, method_name, route_by
            )
            self._event_bus.subscribe(event_class, wrapper)

        host_logger.info(
            "Registered Agent template: %s (scheduled_methods=%d, "
            "event_subscribers=%d)",
            type(agent).__name__,
            len(scheduled_decls),
            len(agent_subscribers),
        )
        return self

    def _make_agent_subscriber_wrapper(
        self,
        template: type[Agent],
        method_name: str,
        route_by: Callable[[Event], Any] | None,
    ) -> Callable[[Event], None]:
        """Build the EventBus wrapper for an Agent ``@subscribes`` method.

        The wrapper is what actually gets subscribed on the host's
        EventBus — not the bound method on the registered template
        prototype. For each delivered Event it:

        1. Either spawns a fresh AgentRun (``route_by is None`` — the
           per-Event default) or finds an existing AgentRun of this
           template whose ``routing_key`` matches ``route_by(event)``,
           spawning one with that key if no match exists.
        2. Resolves the named method on the *live* AgentRun's bound
           instance and invokes it with the Event. Sync methods run
           inline on the publishing thread; async methods are scheduled
           as tasks on the running loop (when one is available).
        3. Isolates :class:`AgentSpawnRejected` (cap-hit) with a warning
           log so other subscribers of the same Event still receive it.
        4. Catches and logs any other subscriber-body exception per
           ADR-0007 — the EventBus's own publish loop also isolates,
           but doing it inside the wrapper means the cap-hit branch and
           the body-error branch are observable separately in tests and
           in production logs.
        """

        def wrapper(event: Event) -> None:
            try:
                if route_by is None:
                    # Spawn-new default: each matching Event mints a
                    # fresh AgentRun. ``triggered_by_event`` lets the
                    # constructor see the Event that birthed it.
                    run = self.spawn(template, triggered_by_event=event)
                else:
                    key = route_by(event)
                    # Find an existing live AgentRun of this template
                    # with a matching routing key. Linear scan is fine —
                    # in-flight counts are bounded (max_concurrent) and
                    # the routing-key index would itself need to be
                    # invalidated on every spawn / natural termination,
                    # which is more bookkeeping than the scan saves.
                    existing: AgentRun | None = None
                    for r in self._agent_runs.values():
                        if r.template is template and r.routing_key == key:
                            existing = r
                            break
                    if existing is None:
                        run = self.spawn(
                            template,
                            triggered_by_event=event,
                            routing_key=key,
                        )
                    else:
                        run = existing
            except AgentSpawnRejected as exc:
                # Cap-hit on Event-triggered spawn. ADR-0008 / ticket #14
                # is explicit: log and drop, no queueing, no retry, no
                # propagation. Other subscribers of this Event still
                # receive it because the EventBus publish loop isolates
                # subscribers from each other.
                host_logger.warning(
                    "Event-triggered spawn rejected for template=%s: %s",
                    template.__name__,
                    exc,
                )
                return
            except Exception:  # noqa: BLE001 — subscriber isolation
                host_logger.exception(
                    "Error spawning/routing AgentRun for template=%s on %s",
                    template.__name__,
                    type(event).__name__,
                )
                return

            # Invoke the decorated method on the live AgentRun's bound
            # instance. ``run.agent`` is the fresh per-spawn object
            # carrying any in-flight state — never the registered
            # template prototype, which would leak state across runs.
            try:
                bound = getattr(run.agent, method_name)
                result = bound(event)
                if inspect.iscoroutine(result):
                    # Async subscriber: schedule on the running loop so
                    # the publish call returns promptly. No loop in this
                    # thread means we cannot fire-and-forget; bridge via
                    # ``asyncio.run`` on a one-shot driver. The path is
                    # the same one ``EventBus.publish`` uses for async
                    # subscribers on the sync publish path.
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.run(_await_coro(result))
                    else:
                        loop.create_task(result)
            except Exception:  # noqa: BLE001 — ADR-0007 isolation
                host_logger.exception(
                    "Error in Agent @subscribes wrapper for template=%s "
                    "method=%s on %s",
                    template.__name__,
                    method_name,
                    type(event).__name__,
                )

        return wrapper

    def _attach_scheduled_methods(self, run: AgentRun) -> None:
        """Register every ``@scheduled`` method on ``run`` with the scheduler.

        Called from the :class:`AgentRun`-construction seam (see
        :meth:`AgentRun.__init__`) so the wiring happens *exactly once
        per AgentRun*, immediately after the run is added to
        ``host._agent_runs``. Idempotent across calls: re-adding the
        same ``(run_id, method)`` pair replaces the previous
        registration (the scheduler's :meth:`add` documents this).

        Returns silently if the template carries no scheduled methods
        — the no-op path is the hot path for Agents that only define
        ``async def run()``.
        """
        decls = self._agent_scheduled_decls.get(run.template)
        if not decls or self._scheduler is None:
            return
        instance = run.agent
        for method_name, schedule in decls:
            bound = getattr(instance, method_name)
            self._scheduler.add(run.id, bound, schedule)
        # Start the scheduler on first use. ``start()`` is idempotent;
        # subsequent spawns just add to the already-running scheduler.
        if not self._scheduler.running:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No running loop yet — defer ``start()`` to the caller's
                # context. This path is rare: tests that drive the
                # scheduler outside an event loop bypass spawn anyway.
                return
            self._scheduler.start()

    def spawn(self, template: type[Agent], **kwargs: Any) -> AgentRun:
        """Spawn a new :class:`AgentRun` of ``template``.

        ``template`` must have been registered via
        ``host.register(SomeAgent())``; otherwise this raises
        :class:`AgentNotRegistered`. ``**kwargs`` are forwarded as
        constructor arguments to a *fresh* instance of ``template`` for
        this run — the registered template instance itself is preserved
        unmodified so future spawns get clean state.

        If the template defines ``async def run(self) -> None``, the
        host schedules it as an :class:`asyncio.Task` on the currently
        running event loop and arranges for natural termination
        (``run()`` returning, or raising) to remove the AgentRun from
        :attr:`agent_runs`.

        **Event-loop constraint (foundation slice).** ``spawn()`` must
        be called from a context where an :class:`asyncio` event loop
        is running in the current thread — e.g., from an ``async def``
        test, an ``async`` handler, or an explicit
        ``asyncio.run(main())`` wrapper. The constraint is documented
        rather than silently bridged so the failure mode is loud; a
        future ticket may add an opt-in host-owned background loop for
        sync callers (mirroring the ``dispatch`` vs ``dispatch_async``
        split).

        Returns the constructed :class:`AgentRun`. The same object also
        appears in :attr:`agent_runs` keyed by its ``id``.
        """
        if template not in self._agent_templates:
            raise AgentNotRegistered(template)

        # Concurrency cap (ticket #11 / ADR-0008). A template that sets
        # ``max_concurrent = N`` rejects the N+1th spawn with
        # ``AgentSpawnRejected`` — no queueing. We count live runs of
        # this *exact* template (subclasses do not share the cap unless
        # they also set their own); the comparison must be ``>=`` because
        # the new run has not been added to ``_agent_runs`` yet.
        if template.max_concurrent is not None:
            live = sum(
                1 for r in self._agent_runs.values() if r.template is template
            )
            if live >= template.max_concurrent:
                raise AgentSpawnRejected(
                    f"max_concurrent={template.max_concurrent} reached for "
                    f"{template.__name__}; {live} run(s) already in flight."
                )

        # Event-trigger metadata (issue #14 / ADR-0008). ``spawn`` accepts
        # arbitrary ``**kwargs`` and forwards them to the template
        # constructor; ``triggered_by_event`` and ``routing_key`` are
        # framework-owned reserved names that flow into :class:`AgentRun`,
        # NOT into ``template(**kwargs)``. We pop them off here so they
        # do not collide with user-defined Agent ``__init__`` signatures.
        triggered_by_event: Event | None = kwargs.pop(
            "triggered_by_event", None
        )
        routing_key: Any = kwargs.pop("routing_key", None)

        # Build a *fresh* instance per spawn. The registered template
        # instance is a prototype — we don't run methods on it directly,
        # because callbacks and state would bleed across concurrent runs
        # of the same template (ADR-0008: per-instance state only in v1).
        instance = template(**kwargs)
        # Per-template ``state_store_factory`` overrides the host default
        # (ticket #12). The factory is called once per spawn so each
        # AgentRun receives an instance the factory chose to hand out —
        # whether that's a fresh store per run or a shared one is the
        # factory's decision, not the framework's.
        if template.state_store_factory is not None:
            state_store: AgentStateStore = template.state_store_factory()
        else:
            state_store = self._default_state_store
        run = AgentRun(
            instance,
            self,
            state_store,
            triggered_by_event=triggered_by_event,
            routing_key=routing_key,
        )
        # Stash the original constructor kwargs on the run so the
        # restart-policy loop (``_run_agent``) can re-spawn with the
        # *same* args. Set as a private attribute — not on AgentRun's
        # documented surface — because the restart loop is the only
        # legitimate consumer. ``run.template`` is already there for the
        # template class itself; this is the per-spawn payload.
        run._spawn_kwargs = dict(kwargs)  # type: ignore[attr-defined]
        # Per-run completion ``asyncio.Event`` so :meth:`shutdown` can
        # ``asyncio.wait`` across every in-flight run with one call.
        # Set in ``_run_agent``'s finally-block. Plain attribute (not on
        # the AgentRun public surface) to keep that primitive minimal.
        run._completed = asyncio.Event()  # type: ignore[attr-defined]
        # Wire the back-references the Agent body uses. Done *before*
        # ``run()`` is scheduled so the coroutine sees a fully-formed
        # context from its very first ``await``.
        instance._host = self
        instance._run = run

        self._agent_runs[run.id] = run
        host_logger.info(
            "Spawned AgentRun %s (template=%s)",
            run.id,
            template.__name__,
        )

        run_method = getattr(instance, "run", None)
        if run_method is not None and inspect.iscoroutinefunction(run_method):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as e:
                # Surface a clear error rather than silently failing — a
                # later ticket can add the host-owned background loop.
                # Roll back the registry entry so a failed spawn leaves
                # no orphan in ``agent_runs``.
                del self._agent_runs[run.id]
                raise RuntimeError(
                    "host.spawn() requires a running event loop in the "
                    "calling thread for Agents with an async run() "
                    "method; call spawn() from inside an async context."
                ) from e
            task = loop.create_task(self._run_agent(run, run_method))
            self._agent_tasks[run.id] = task

        return run

    async def _run_agent(
        self,
        run: AgentRun,
        run_method: Callable[[], Any],
    ) -> None:
        """Drive an AgentRun's ``run()`` coroutine and clean up on exit.

        Wraps ``run_method()`` so that *any* exit path — natural return,
        cooperative-stop, or unhandled exception — removes the run from
        :attr:`agent_runs`.

        Failure policy (ticket #11 / ADR-0008):

        - **Cooperative-stop exit** (``run._stop_requested`` is true when
          ``run_method`` returns or raises ``asyncio.CancelledError``):
          terminate quietly, no :class:`AgentFailed`, no restart.
        - **Unhandled exception**: publish :class:`AgentFailed` carrying
          the exception and the run id, then honour
          ``template.restart_policy`` if set — re-spawn a fresh AgentRun
          with the original constructor kwargs, sleeping per the policy's
          backoff between attempts, until ``policy.max_retries`` is
          exhausted. ``RetryPolicy.should_retry`` is the single source of
          truth for retryable-ness; framework signals
          (:class:`PyModulesSignal`) and non-matching exception types are
          treated as non-retryable just as they are in the dispatch chain.
        """
        unhandled: BaseException | None = None
        cancelled_cooperatively = False
        try:
            await run_method()
        except asyncio.CancelledError:
            # Two flavours of CancelledError reach this frame:
            # (1) ``shutdown()`` hard-cancelled the task after grace —
            #     the cooperative-stop flag will be set, but the run
            #     never honoured it. We treat this as cooperative-exit
            #     here; the AgentFailed(AgentRunStuck) publish is the
            #     caller's responsibility (``shutdown()``), not ours,
            #     because only the caller knows it had to hard-cancel.
            # (2) Some other caller cancelled the task. Same treatment —
            #     no AgentFailed, no restart. ``asyncio.CancelledError``
            #     is a control-flow signal, not a service failure.
            cancelled_cooperatively = True
        except Exception as e:  # noqa: BLE001 — bookkeeping must not leak
            unhandled = e
            host_logger.exception(
                "Unhandled error in AgentRun %s (template=%s); terminating run",
                run.id,
                run.template.__name__,
            )
        finally:
            # Persist terminal state before deregistering the run, so
            # observability tooling can inspect the final snapshot via
            # the store after the AgentRun is gone (ticket #12 / ADR-0008).
            # Best-effort: a store failure here must not leak past the
            # bookkeeping ``finally`` and prevent registry cleanup.
            if run._state_store is not None:
                try:
                    run._state_store.set(run.id, run.state)
                except Exception:  # noqa: BLE001 — terminal write is best-effort
                    host_logger.exception(
                        "Failed to persist terminal state for AgentRun %s",
                        run.id,
                    )
            self._agent_runs.pop(run.id, None)
            self._agent_tasks.pop(run.id, None)
            # Signal completion to ``shutdown()`` and to any other waiter
            # before publishing AgentFailed / scheduling restarts — those
            # downstream activities must not gate the run's "I'm done"
            # observability.
            try:
                run._completed.set()  # type: ignore[attr-defined]
            except AttributeError:
                # AgentRuns constructed via the test seam (without going
                # through ``spawn()``) may not carry the attribute.
                pass
            host_logger.info(
                "AgentRun %s (template=%s) terminated",
                run.id,
                run.template.__name__,
            )

        # If the body exited cooperatively (stop honoured, or task
        # cancelled), do not publish AgentFailed and do not restart.
        if unhandled is None or cancelled_cooperatively or run._stop_requested:
            return

        # Publish ``AgentFailed`` for the run that just died.
        self._publish_agent_failed(run, unhandled)

        # Honour the template's ``restart_policy``. We deliberately track
        # the attempt counter on the AgentRun (``_restart_attempt``) and
        # propagate it onto the next spawned run, so the restart chain
        # has a single global cap of ``policy.max_retries`` re-spawns —
        # not a per-spawn cap that would multiply geometrically. The
        # restart attempt counter is initialised lazily; a freshly-spawned
        # run sees attribute-absent → attempt 0.
        policy = run.template.restart_policy
        if policy is None:
            return

        prior_attempts: int = getattr(run, "_restart_attempt", 0)
        # ``should_retry`` returns False once ``attempt >= max_retries``;
        # we pass ``prior_attempts`` because that's how many restarts
        # have *already* happened in this chain. If ``prior_attempts``
        # is N, ``should_retry`` decides whether to allow attempt N+1.
        if not (
            isinstance(unhandled, Exception)
            and policy.should_retry(unhandled, prior_attempts)
        ):
            # Exhausted or non-retryable exception type. The AgentFailed
            # already published above is the terminal one — nothing more
            # to do here.
            host_logger.info(
                "AgentRun restart chain ended for template=%s after %d attempt(s)",
                run.template.__name__,
                prior_attempts,
            )
            return

        delay = policy.calculate_delay(prior_attempts)
        host_logger.warning(
            "Restarting AgentRun for template=%s (attempt %d/%d) after %.2fs: %s",
            run.template.__name__,
            prior_attempts + 1,
            policy.max_retries,
            delay,
            unhandled,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # Host is being torn down mid-backoff — abort the restart
            # chain without further publishes.
            return

        spawn_kwargs: dict[str, Any] = getattr(run, "_spawn_kwargs", {}) or {}
        try:
            new_run = self.spawn(run.template, **spawn_kwargs)
        except AgentSpawnRejected as e:
            self._publish_agent_failed(run, e)
            return
        # Propagate the attempt counter onto the restarted run. Without
        # this, the new run's ``_run_agent`` would see ``_restart_attempt``
        # absent → 0, and would allow another full ``max_retries`` worth
        # of restarts. ``+1`` because this restart is now in flight.
        new_run._restart_attempt = prior_attempts + 1  # type: ignore[attr-defined]

    def _publish_agent_failed(
        self, run: AgentRun, error: BaseException
    ) -> None:
        """Publish an :class:`AgentFailed` Event for ``run`` carrying ``error``.

        Pulled out of ``_run_agent`` so :meth:`shutdown` can call it
        directly for hard-cancelled runs (where the publish happens from
        the shutdown thread, not the per-run task).
        """
        event = AgentFailed(
            agent_template_name=run.template.__name__,
            agent_run_id=run.id,
            error=error,
        )
        try:
            self.publish(event)
        except Exception:  # noqa: BLE001 — bookkeeping must not leak
            host_logger.exception(
                "Failed to publish AgentFailed for AgentRun %s", run.id
            )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _invoke_chain(self, command: Command[Any, Any]) -> Any:
        """
        Run the composed middleware chain, applying the host's
        ``propagate_exceptions`` policy at the outermost layer.

        Exceptions from middleware or the terminal handler are wrapped in
        ``CommandHandlingError`` so callers always see a uniform type.
        With ``propagate_exceptions=False`` the exception is logged and
        dispatch returns ``None``.
        """
        try:
            return await self._chain(command)
        except PyModulesSignal:
            # Framework-level signals (rate limit, breaker open, unknown
            # command, …) always propagate as-is, regardless of
            # ``propagate_exceptions`` (which controls whether *handler*
            # exceptions escape).
            raise
        except CommandHandlingError:
            if self._config.propagate_exceptions:
                raise
            host_logger.error("Dispatch of %s failed", command.name, exc_info=True)
            return None
        except Exception as e:
            handler = self._dispatch_table.get(type(command))
            module = handler.__self__ if handler is not None else None  # type: ignore[attr-defined]
            wrapped = CommandHandlingError(
                f"Handler error in {module.metadata.name if module else '<unknown>'}",
                command=command,
                module=module,
                original_error=e,
            )
            wrapped.__cause__ = e
            if self._config.propagate_exceptions:
                raise wrapped from e
            host_logger.error("Dispatch of %s failed: %s", command.name, e, exc_info=True)
            return None

    def dispatch(self, command: Command[Req, Resp]) -> Resp:
        """
        Synchronously dispatch ``command`` through the middleware chain.

        Thin wrapper that does not bridge async:

        - If the resolved handler is a coroutine function, raises
          ``SyncDispatchOnAsyncHandlerError``.
        - If an event loop is already running in this thread, raises
          ``SyncDispatchInAsyncContextError``.

        Otherwise runs the async chain to completion via ``asyncio.run``.

        Returns:
            The response value returned by the claiming handler, or
            ``None`` if no handler claims ``type(command)``.
        """
        handler = self._dispatch_table.get(type(command))
        if handler is not None and inspect.iscoroutinefunction(handler):
            raise SyncDispatchOnAsyncHandlerError(
                f"Sync dispatch() cannot run async handler for {type(command).__name__}; "
                "use dispatch_async() instead."
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SyncDispatchInAsyncContextError(
                "Sync dispatch() cannot run while an event loop is already running "
                "in this thread; use dispatch_async() instead."
            )

        host_logger.debug("Dispatching command: %s", command.name)
        return asyncio.run(self._invoke_chain(command))  # type: ignore[no-any-return]

    async def dispatch_async(self, command: Command[Req, Resp]) -> Resp:
        """
        Asynchronously dispatch ``command`` through the middleware chain.

        Returns:
            The response value returned by the claiming handler, or
            ``None`` if no handler claims ``type(command)``.
        """
        host_logger.debug("Dispatching command async: %s", command.name)
        return await self._invoke_chain(command)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Event publishing
    # ------------------------------------------------------------------

    def publish(self, event: Event) -> None:
        """
        Publish ``event`` to every in-process subscriber, synchronously.

        Thin facade over ``self.event_bus.publish``. Modules call this
        explicitly inside a handler (or from any other in-process site);
        the framework never auto-publishes after a Command succeeds.

        Subscriber exceptions are isolated — a raise in one subscriber is
        logged and swallowed; other subscribers still receive the event.
        This is in-process delivery only; cross-process broadcast remains
        a Module-owned broker concern.
        """
        self._event_bus.publish(event)

    async def publish_async(self, event: Event) -> None:
        """
        Publish ``event`` to every in-process subscriber, awaiting async ones.

        Thin facade over ``self.event_bus.publish_async``. Use this from
        inside ``async def`` handlers so async subscribers' coroutines
        are awaited on the calling loop.
        """
        await self._event_bus.publish_async(event)

    # ------------------------------------------------------------------
    # Module lookup
    # ------------------------------------------------------------------

    def get_module(self, module_type: type[Module]) -> Module | None:
        """Find a registered module by type."""
        for module in self._modules:
            if isinstance(module, module_type):
                return module
        return None

    def get_module_by_name(self, name: str) -> Module | None:
        """Find a registered module by name."""
        for module in self._modules:
            if module.metadata.name == name:
                return module
        return None

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the host: stop AgentRuns, unregister modules, shut down the executor.

        Sequence (ticket #11 / ADR-0008):

        1. Set ``_stop_requested = True`` on every in-flight AgentRun so
           ``run()`` bodies that poll the flag exit cooperatively.
        2. Wait up to ``config.shutdown_grace`` seconds for the
           per-run completion events to fire.
        3. For any run still alive after grace expires, hard-cancel its
           asyncio task and publish :class:`AgentFailed` with
           :class:`AgentRunStuck` as the error so observability tooling
           can distinguish a stuck run from a natural exception.
        4. Unregister Modules, clear the EventBus, shut down the executor.

        The asyncio bridging is best-effort: if the host is being shut
        down from a non-async context with no event loop, we skip the
        grace-period wait (there is no task to hard-cancel anyway —
        the in-flight AgentRuns are scheduled on a loop the caller knows
        about; the synchronous shutdown is a teardown signal).
        """
        host_logger.info("Shutting down ModuleHost")

        # If a Scheduler was lazily constructed (issue #13), stop it
        # before draining AgentRuns so no fresh ``@scheduled`` ticks
        # land mid-shutdown. ``self._scheduler`` is set up by #13's
        # registration path; we only know it via the lazy attribute,
        # so a host that never registered a scheduled Agent skips this
        # branch without importing :mod:`pymodules.scheduler`.
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:  # noqa: BLE001 — bookkeeping must not leak
                host_logger.exception(
                    "Error stopping scheduler during host shutdown"
                )

        # Snapshot the in-flight runs *before* requesting stop — the
        # dict mutates during teardown via ``_run_agent``'s finally-block.
        in_flight = list(self._agent_runs.values())
        for run in in_flight:
            run._stop_requested = True

        if in_flight:
            self._await_agent_shutdown(in_flight)

        for module in self._modules.copy():
            self.unregister(module)
        # Drop any free-function subscriptions that bypassed the @subscribes
        # path; unregister() above only removes Module-bound handlers.
        self._event_bus.clear()
        self._executor.shutdown(wait=wait)
        host_logger.debug("ModuleHost shutdown complete")

    def _await_agent_shutdown(self, in_flight: list[AgentRun]) -> None:
        """Bridge :meth:`shutdown` (sync) to the per-run completion events.

        Pulled into its own method so the asyncio plumbing — finding a
        loop or building one — does not clutter ``shutdown``. The grace
        period is consulted from ``self._config.shutdown_grace``.

        Resolution order:

        - If a loop is currently running in this thread, schedule the
          wait as a coroutine on it via ``asyncio.run_coroutine_threadsafe``
          — but this is rare for a sync ``shutdown()`` call. More commonly,
          the loop the AgentRuns were spawned on is *the same* loop the
          caller is running, and ``shutdown()`` was called from a
          synchronous teardown helper (e.g., a pytest fixture). In that
          case the loop is not running in the calling thread; we use
          ``asyncio.run`` on a small driver coroutine.
        - If no loop is reachable at all (the runs' tasks are orphans),
          fall back to a short busy-wait on the per-run completion events
          using ``threading`` since ``asyncio.Event`` does not expose a
          thread-safe wait. In that degenerate case we simply set the
          stop flag and move on — hard-cancel needs a live loop, which
          we do not have.
        """
        grace = self._config.shutdown_grace

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is None:
            # No loop in *this* thread. Drive the wait via a fresh
            # ``asyncio.run`` on a driver coroutine that finds each
            # run's task on its own loop. We assume — and the spawn
            # contract documents — that AgentRuns with async ``run()``
            # were created on a loop visible to the current thread; if
            # the caller ran a separate loop on another thread, the
            # tasks are on that loop and our driver here cannot reach
            # them. We still set ``_stop_requested`` (already done by
            # caller), then run the driver to settle the futures on
            # whatever loop we can build.
            try:
                asyncio.run(self._shutdown_drive(in_flight, grace))
            except RuntimeError as exc:
                # Edge: ``asyncio.run`` refuses if a loop *is* running
                # elsewhere in this thread. Skip the grace-period wait;
                # the stop flag is set and any future natural checkpoint
                # will honour it.
                host_logger.warning(
                    "ModuleHost.shutdown: could not drive grace-period "
                    "wait (%s); falling back to set-stop-only.",
                    exc,
                )
            return

        # Loop already running in this thread — synchronous shutdown()
        # while inside async code is the asyncio antipattern, but we
        # cope: schedule the driver as a task and busy-wait on it via
        # the loop's run-until-complete is impossible (loop is running).
        # Best we can do is fire-and-forget the driver; the caller
        # already has the loop's attention and can ``await`` the
        # AgentRuns to settle themselves before calling shutdown().
        host_logger.warning(
            "ModuleHost.shutdown called from inside a running event loop; "
            "AgentRun cooperative-stop has been requested but the grace "
            "period cannot be awaited synchronously. Call shutdown from "
            "a sync context, or await each run.stop() yourself first."
        )

    async def _shutdown_drive(
        self, in_flight: list[AgentRun], grace: float
    ) -> None:
        """Async driver for the cooperative-stop wait + hard-cancel pass.

        Run via ``asyncio.run`` from :meth:`_await_agent_shutdown`.
        """
        # ``asyncio.run`` builds a new event loop. The in-flight tasks
        # were created on *their* original loop, not this one — so a
        # straight ``await run._completed.wait()`` won't actually unblock
        # them, because their tasks aren't being driven here.
        #
        # In practice, in-process tests and synchronous teardown helpers
        # invoke ``shutdown()`` after the original loop has stopped, so
        # the tasks are already in a finalised state and ``_completed``
        # is already set. The wait below short-circuits in that case.
        #
        # For runs that are *not* yet settled, we cannot drive their
        # tasks from a foreign loop; we observe their ``_completed``
        # status via the cross-loop poll the wait_for primitive provides.
        deadline_tasks: list[asyncio.Task[Any]] = []
        for run in in_flight:
            event = getattr(run, "_completed", None)
            if event is None:
                continue
            deadline_tasks.append(
                asyncio.create_task(self._wait_for_run_completion(run, grace))
            )
        if deadline_tasks:
            await asyncio.gather(*deadline_tasks, return_exceptions=True)

    async def _wait_for_run_completion(
        self, run: AgentRun, grace: float
    ) -> None:
        """Wait up to ``grace`` seconds for ``run`` to terminate; hard-cancel if not.

        Polls ``run._completed`` (an :class:`asyncio.Event` on the run's
        original loop) by checking ``is_set()`` against a deadline,
        because awaiting the event directly would require running on
        that same loop.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        completed: asyncio.Event = run._completed
        # Tight-ish poll loop: 10ms tick is fast enough for tests, cheap
        # enough for production teardown. Using a fixed tick (rather
        # than an exponential one) keeps the worst-case wakeup latency
        # bounded so a fast-exiting Agent terminates ``shutdown()``
        # promptly.
        while not completed.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.01)

        if completed.is_set():
            return

        # Grace expired and the run is still alive. Hard-cancel.
        task = self._agent_tasks.get(run.id)
        host_logger.warning(
            "AgentRun %s (template=%s) did not honour stop within %.2fs; "
            "hard-cancelling",
            run.id,
            run.template.__name__,
            grace,
        )
        if task is not None and not task.done():
            task.cancel()
        # Publish AgentFailed(AgentRunStuck). The per-run task may also
        # publish from its own ``_run_agent`` frame, but only if its
        # body raised before the cancel landed — we hold the canonical
        # "stuck" record here because only ``shutdown()`` knows that
        # the hard-cancel was driven by grace expiry.
        stuck = AgentRunStuck(
            f"AgentRun {run.id} did not honour stop within {grace:.2f}s"
        )
        self._publish_agent_failed(run, stuck)


async def _await_coro(coro: Any) -> None:
    """Trivial coroutine wrapper so we can pass any awaitable to ``asyncio.run``.

    Used by the Agent ``@subscribes`` wrapper when an async-decorated
    callback fires from a sync publish path and no running loop is
    available in the current thread. Mirrors the same pattern in
    :mod:`pymodules.eventbus`.
    """
    await coro


__all__ = ["ModuleHost"]
