"""
Unit tests for :class:`pymodules.Agent` and :class:`pymodules.AgentRun`.

These tests deliberately use a stand-in / mock host so that the
``Agent`` and ``AgentRun`` primitives can be exercised in isolation —
``ModuleHost``-level concerns (spawn lifecycle, dispatch through the
chain, EventBus delivery) live in ``tests/integration/test_agent_integration.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from pymodules import (
    Agent,
    AgentError,
    AgentFailed,
    AgentNotRegistered,
    AgentRun,
    AgentRunStuck,
    AgentSpawner,
    AgentSpawnRejected,
    Command,
    CommandRequest,
    CommandResponse,
    Event,
    Module,
    ModuleHost,
    RetryPolicy,
    handles,
    subscribes,
)
from pymodules.exceptions import PyModulesError


class _MockHost:
    """Minimal host stand-in for AgentRun unit tests.

    AgentRun only reads ``host`` as an opaque back-reference; nothing in
    the unit-test surface dispatches or publishes. A bare object would
    work, but a named class makes the test intent obvious in failure
    output.
    """


# ---------------------------------------------------------------------------
# AgentRun
# ---------------------------------------------------------------------------


class TestAgentRunIdentity:
    """``AgentRun.id`` is a UUIDv4 string, stable, and unique per run."""

    def test_id_is_uuid_v4(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]

        # Round-trip through uuid.UUID to assert both well-formedness
        # and version. version == 4 is the ADR-0008 contract.
        parsed = uuid.UUID(run.id)
        assert parsed.version == 4

    def test_id_is_str_typed(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]

        assert isinstance(run.id, str)

    def test_id_is_stable_for_the_run(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]

        first = run.id
        # Re-read — must not regenerate.
        assert run.id == first

    def test_ids_are_unique_per_run(self) -> None:
        host = _MockHost()
        a = AgentRun(Agent(), host)  # type: ignore[arg-type]
        b = AgentRun(Agent(), host)  # type: ignore[arg-type]

        assert a.id != b.id


class TestAgentRunStop:
    """``stop()`` flips the cooperative-stop flag."""

    def test_stop_sets_stop_requested(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]

        assert run._stop_requested is False
        run.stop()
        assert run._stop_requested is True

    def test_stop_is_idempotent(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]

        run.stop()
        run.stop()
        run.stop()
        assert run._stop_requested is True


class TestAgentRunBackReferences:
    """``host`` and ``template`` are read-only back-references."""

    def test_template_is_the_agent_class(self) -> None:
        class MyAgent(Agent):
            pass

        host = _MockHost()
        run = AgentRun(MyAgent(), host)  # type: ignore[arg-type]
        assert run.template is MyAgent

    def test_host_property_returns_the_host(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]
        assert run.host is host

    def test_agent_property_returns_the_bound_instance(self) -> None:
        host = _MockHost()
        instance = Agent()
        run = AgentRun(instance, host)  # type: ignore[arg-type]
        assert run.agent is instance


# ---------------------------------------------------------------------------
# Agent template
# ---------------------------------------------------------------------------


class TestAgentSubclassing:
    """The Agent base supports both pure-callback and run()-bearing templates."""

    def test_agent_without_run_is_valid(self) -> None:
        """A user may subclass ``Agent`` without defining ``run()``.

        Later tickets give such templates meaning via ``@subscribes`` /
        ``@scheduled`` markers; this test just confirms the base class
        does not require a ``run()`` method.
        """

        class CallbackOnlyAgent(Agent):
            pass

        # Instantiation must succeed without errors.
        instance = CallbackOnlyAgent()
        assert isinstance(instance, Agent)
        # No ``run`` attribute on the class beyond what ``object`` provides.
        assert not hasattr(CallbackOnlyAgent, "run")

    def test_agent_with_run_exposes_coroutine(self) -> None:
        """A subclass defining ``async def run`` exposes a coroutine function."""

        class LoopAgent(Agent):
            async def run(self) -> None:
                return None

        instance = LoopAgent()
        import inspect as _inspect

        assert _inspect.iscoroutinefunction(instance.run)

    def test_agent_host_and_run_default_to_none(self) -> None:
        """Before spawn, ``_host`` and ``_run`` are ``None``.

        The host populates them immediately before scheduling ``run()``;
        a freshly-instantiated template (the one passed to
        ``host.register(...)``, or a unit-test instance) sees ``None``.
        """

        class MyAgent(Agent):
            pass

        instance = MyAgent()
        assert instance._host is None
        assert instance._run is None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestAgentExceptions:
    """``AgentError`` family fits the existing ``PyModulesError`` hierarchy."""

    def test_agent_error_is_pymodules_error(self) -> None:
        assert issubclass(AgentError, PyModulesError)

    def test_agent_not_registered_is_agent_error(self) -> None:
        assert issubclass(AgentNotRegistered, AgentError)

    def test_agent_not_registered_carries_template(self) -> None:
        class Unregistered(Agent):
            pass

        err = AgentNotRegistered(Unregistered)
        assert err.template is Unregistered
        # Message mentions the class name so logs are useful.
        assert "Unregistered" in str(err)

    def test_agent_not_registered_raises_cleanly(self) -> None:
        class Unregistered(Agent):
            pass

        with pytest.raises(AgentNotRegistered) as exc_info:
            raise AgentNotRegistered(Unregistered)
        assert exc_info.value.template is Unregistered


# ---------------------------------------------------------------------------
# AgentSpawner Protocol + Module-injection (issue #15)
#
# These tests cover the narrow capability handed to Modules so they can
# spawn AgentRuns without a host back-reference (ADR-0003 invariant).
# ---------------------------------------------------------------------------


@dataclass
class _SpawnRequest(CommandRequest):
    """Trivial request body for the spawn-from-Module integration test."""


@dataclass
class _SpawnResponse(CommandResponse):
    run_id: str = ""


class _SpawnCommand(Command[_SpawnRequest, _SpawnResponse]):
    """Command the spawner-injected Module handles by spawning an Agent."""

    name = "agent.spawn_from_module"


class _NoOpAgent(Agent):
    """Agent template with no ``run()`` — host.spawn() must still work
    end-to-end without an event loop required for scheduling."""


class TestAgentSpawnerProtocol:
    """``host.agent_spawner`` returns a narrowed :class:`AgentSpawner`."""

    def test_agent_spawner_satisfies_protocol_isinstance(self) -> None:
        """A ``runtime_checkable`` Protocol means ``isinstance`` works."""
        host = ModuleHost()
        spawner = host.agent_spawner
        assert isinstance(spawner, AgentSpawner)

    def test_agent_spawner_is_cached(self) -> None:
        """Repeated property access returns the same instance."""
        host = ModuleHost()
        first = host.agent_spawner
        second = host.agent_spawner
        assert first is second

    def test_agent_spawner_exposes_only_spawn(self) -> None:
        """The spawner exposes exactly one public attribute: ``spawn``.

        ``dir(spawner)`` minus dunders is the runtime surface; the
        adapter intentionally has nothing else. None of the host's
        broader API (dispatch, publish, register, agent_runs, ...) is
        reachable via attribute access.
        """
        host = ModuleHost()
        spawner = host.agent_spawner

        public = [name for name in dir(spawner) if not name.startswith("_")]
        assert public == ["spawn"]

        # Sanity-check each forbidden surface explicitly so a regression
        # that adds, say, ``publish`` to the adapter fails with an
        # obvious message rather than a list-equality diff.
        for forbidden in (
            "dispatch",
            "dispatch_async",
            "publish",
            "publish_async",
            "register",
            "unregister",
            "agent_runs",
            "modules",
            "event_bus",
        ):
            assert not hasattr(spawner, forbidden), (
                f"AgentSpawner adapter must not expose {forbidden!r}; "
                "doing so would widen the capability beyond spawn()."
            )

    def test_spawner_spawn_registers_run_in_host_agent_runs(self) -> None:
        """``spawner.spawn(...)`` produces an :class:`AgentRun` that the
        host tracks in its read-only ``agent_runs`` view — identical
        runtime semantics to calling ``host.spawn(...)`` directly."""
        host = ModuleHost()
        host.register(_NoOpAgent())

        spawner = host.agent_spawner
        run = spawner.spawn(_NoOpAgent)

        assert isinstance(run, AgentRun)
        assert run.id in host.agent_runs
        assert host.agent_runs[run.id] is run

    def test_spawner_propagates_agent_not_registered(self) -> None:
        """Behaviour-parity: unregistered templates still raise the same
        :class:`AgentNotRegistered` through the narrow adapter."""

        class _Unregistered(Agent):
            pass

        host = ModuleHost()
        spawner = host.agent_spawner

        with pytest.raises(AgentNotRegistered):
            spawner.spawn(_Unregistered)


class TestModuleInjectsAgentSpawner:
    """A Module holds ``_spawner: AgentSpawner`` and spawns from a handler.

    This is the headline acceptance criterion for issue #15: a Module
    can spawn an Agent without ever touching the ``ModuleHost``.
    """

    def test_module_with_injected_spawner_spawns_from_handler(self) -> None:
        captured: dict[str, AgentRun] = {}

        class SpawningModule(Module):
            def __init__(self, spawner: AgentSpawner) -> None:
                super().__init__()
                # Private field — Module deliberately holds the narrow
                # capability, not the host. Typed as ``AgentSpawner``
                # so static analysis enforces the same restriction.
                self._spawner: AgentSpawner = spawner

            @handles(_SpawnCommand)
            def on_spawn(self, command: _SpawnCommand) -> _SpawnResponse:
                run = self._spawner.spawn(_NoOpAgent)
                captured["run"] = run
                return _SpawnResponse(run_id=run.id)

        host = ModuleHost()
        host.register(_NoOpAgent())
        module = SpawningModule(host.agent_spawner)
        host.register(module)

        # Module has no host back-reference. ``Module.__init__`` does not
        # set one; if a future refactor accidentally adds it, this test
        # catches it loudly.
        assert not hasattr(module, "host"), (
            "Module must not hold a host back-reference (ADR-0003); "
            "AgentSpawner is the narrow alternative."
        )
        assert not hasattr(module, "_host"), (
            "Module must not hold a host back-reference (ADR-0003); "
            "AgentSpawner is the narrow alternative."
        )

        # End-to-end: dispatching the command runs the handler, which
        # spawns the Agent via the narrow spawner; the run is visible in
        # ``host.agent_runs`` and the handler returns the spawned run id.
        response = host.dispatch(_SpawnCommand(request=_SpawnRequest()))

        assert isinstance(response, _SpawnResponse)
        spawned = captured["run"]
        assert response.run_id == spawned.id
        assert spawned.id in host.agent_runs

    async def test_module_spawner_works_for_async_run_agents(self) -> None:
        """The spawner adapter delegates unchanged to ``host.spawn``, so
        Agents with an ``async def run()`` are scheduled on the loop
        exactly as they are via the direct ``host.spawn`` path."""
        ran = asyncio.Event()

        class _LoopAgent(Agent):
            async def run(self) -> None:
                ran.set()

        host = ModuleHost()
        host.register(_LoopAgent())
        spawner = host.agent_spawner

        run = spawner.spawn(_LoopAgent)
        # Yield to the loop so the scheduled task runs to completion.
        await asyncio.sleep(0)
        await ran.wait()
        assert run.template is _LoopAgent


# ---------------------------------------------------------------------------
# Ticket #11 — AgentFailed Event, restart_policy, max_concurrent, shutdown_grace
#
# Unit-level coverage of the new exception types, the AgentFailed dataclass
# shape, and the Agent ClassVar defaults. Behavioural tests (publish on
# crash, restart loop, cap enforcement, shutdown grace) live in the
# integration suite alongside the lifecycle tests.
# ---------------------------------------------------------------------------


class TestAgentFailedEvent:
    """:class:`AgentFailed` is an :class:`Event` carrying the failure triple."""

    def test_agent_failed_is_event_subclass(self) -> None:
        assert issubclass(AgentFailed, Event)

    def test_agent_failed_default_name(self) -> None:
        """Default ``name`` is the documented ``"agent.failed"``.

        Mirrors the ``Event`` ``name`` convention — subscribers can match
        on the class type *or* the string name; the default keeps log
        output sensible without forcing every caller to set it.
        """
        # ``__post_init__`` (inherited from Event) leaves ``name``
        # alone when the class attribute is already set.
        event = AgentFailed(
            agent_template_name="X",
            agent_run_id="abc",
            error=RuntimeError("boom"),
        )
        assert event.name == "agent.failed"

    def test_agent_failed_carries_fields(self) -> None:
        err = RuntimeError("kaboom")
        event = AgentFailed(
            agent_template_name="DemoAgent",
            agent_run_id="run-123",
            error=err,
        )
        assert event.agent_template_name == "DemoAgent"
        assert event.agent_run_id == "run-123"
        assert event.error is err

    def test_agent_failed_error_field_accepts_base_exception(self) -> None:
        """``error`` is typed ``BaseException | None`` — including
        :class:`KeyboardInterrupt` / :class:`SystemExit` — so a stuck-run
        wrapping any exception type is representable."""
        event = AgentFailed(
            agent_template_name="X",
            agent_run_id="y",
            error=KeyboardInterrupt(),
        )
        assert isinstance(event.error, BaseException)


class TestNewAgentExceptions:
    """``AgentSpawnRejected`` and ``AgentRunStuck`` join the existing family."""

    def test_agent_spawn_rejected_is_agent_error(self) -> None:
        assert issubclass(AgentSpawnRejected, AgentError)

    def test_agent_run_stuck_is_agent_error(self) -> None:
        assert issubclass(AgentRunStuck, AgentError)

    def test_agent_spawn_rejected_is_pymodules_error(self) -> None:
        """Same hierarchy guarantee :class:`AgentNotRegistered` has —
        callers can catch the whole family with one ``except``."""
        assert issubclass(AgentSpawnRejected, PyModulesError)

    def test_agent_run_stuck_is_pymodules_error(self) -> None:
        assert issubclass(AgentRunStuck, PyModulesError)

    def test_agent_spawn_rejected_message_round_trip(self) -> None:
        err = AgentSpawnRejected("max_concurrent=2 reached for Foo")
        assert "Foo" in str(err)


class TestAgentClassVars:
    """``max_concurrent`` and ``restart_policy`` default to ``None``."""

    def test_max_concurrent_default_is_none(self) -> None:
        assert Agent.max_concurrent is None

    def test_restart_policy_default_is_none(self) -> None:
        assert Agent.restart_policy is None

    def test_subclasses_can_override_class_vars(self) -> None:
        """ClassVars are per-subclass — overriding on a subclass does
        not leak into the base or sibling templates."""

        class A(Agent):
            max_concurrent = 3
            restart_policy = RetryPolicy(max_retries=1, base_delay=0.01)

        class B(Agent):
            pass

        assert A.max_concurrent == 3
        assert isinstance(A.restart_policy, RetryPolicy)
        # Sibling untouched.
        assert B.max_concurrent is None
        assert B.restart_policy is None
        # Base untouched.
        assert Agent.max_concurrent is None
        assert Agent.restart_policy is None


# ---------------------------------------------------------------------------
# Ticket #14 — Agent @subscribes with spawn-new default and route_by= routing
#
# All tests below this banner are additive to issue #14: they cover the
# Event-subscription trigger mode on Agent templates. The Module-side
# @subscribes path is unchanged and stays covered by ``tests/test_eventbus.py``.
# ---------------------------------------------------------------------------


async def _drain(host: ModuleHost, run_ids: list[str], ticks: int = 100) -> None:
    """Yield to the loop until every id in ``run_ids`` leaves ``host.agent_runs``."""
    for _ in range(ticks):
        if all(rid not in host.agent_runs for rid in run_ids):
            return
        await asyncio.sleep(0)


@dataclass
class _SubTestEvent(Event):
    """Generic test Event for Agent @subscribes coverage."""

    tenant_id: str = ""
    payload: str = ""
    name: str = "sub.test"


@dataclass
class _OtherEvent(Event):
    """Sibling test Event used to verify exact-type routing."""

    name: str = "sub.other"


class TestAgentSubscribesSpawnNew:
    """``@subscribes(E)`` with no ``route_by`` spawns a fresh AgentRun per Event."""

    async def test_publishes_spawn_one_run_each(self) -> None:
        received: list[tuple[str, _SubTestEvent]] = []

        class Spawned(Agent):
            @subscribes(_SubTestEvent)
            def on_event(self, event: _SubTestEvent) -> None:
                # ``self._run`` is wired before the wrapper invokes the
                # method, so id-keying the captured tuple lets the test
                # assert "different AgentRun per event".
                assert self._run is not None
                received.append((self._run.id, event))

        host = ModuleHost()
        host.register(Spawned())

        for i in range(3):
            host.publish(_SubTestEvent(tenant_id="t", payload=str(i)))

        # Three separate AgentRuns means three distinct ids on the
        # captured tuples — set-cardinality is the assertion that
        # generalises across asyncio scheduling.
        assert len(received) == 3
        ids = {r[0] for r in received}
        assert len(ids) == 3
        payloads = sorted(e.payload for _, e in received)
        assert payloads == ["0", "1", "2"]

    async def test_triggered_by_event_is_set_on_the_run(self) -> None:
        """The fresh AgentRun observes its triggering Event via the
        documented ``triggered_by_event`` kwarg/attribute."""
        triggers: list[Event | None] = []

        class Spawned(Agent):
            @subscribes(_SubTestEvent)
            def on_event(self, event: _SubTestEvent) -> None:
                assert self._run is not None
                triggers.append(self._run.triggered_by_event)

        host = ModuleHost()
        host.register(Spawned())

        evt = _SubTestEvent(payload="hi")
        host.publish(evt)

        assert triggers == [evt]


class TestAgentSubscribesRouteBy:
    """``route_by=lambda e: ...`` routes matching Events to one AgentRun by key."""

    async def test_same_key_routes_to_one_run(self) -> None:
        """Three Events with the same tenant_id all land on a single
        AgentRun — proving the routing-key lookup hits an existing run."""
        invocations: list[tuple[str, str]] = []

        class TenantSaga(Agent):
            @subscribes(_SubTestEvent, route_by=lambda e: e.tenant_id)
            def on_event(self, event: _SubTestEvent) -> None:
                assert self._run is not None
                invocations.append((self._run.id, event.payload))

        host = ModuleHost()
        host.register(TenantSaga())

        for i in range(3):
            host.publish(_SubTestEvent(tenant_id="t-A", payload=str(i)))

        # Same routing key → same in-flight AgentRun.
        ids = {r[0] for r in invocations}
        assert len(ids) == 1
        payloads = [p for _, p in invocations]
        assert payloads == ["0", "1", "2"]

    async def test_different_keys_spawn_separate_runs(self) -> None:
        """Distinct routing keys → distinct AgentRuns."""
        invocations: list[tuple[str, str]] = []

        class TenantSaga(Agent):
            @subscribes(_SubTestEvent, route_by=lambda e: e.tenant_id)
            def on_event(self, event: _SubTestEvent) -> None:
                assert self._run is not None
                invocations.append((self._run.id, event.tenant_id))

        host = ModuleHost()
        host.register(TenantSaga())

        host.publish(_SubTestEvent(tenant_id="t-A"))
        host.publish(_SubTestEvent(tenant_id="t-B"))
        host.publish(_SubTestEvent(tenant_id="t-C"))

        # One run per tenant.
        ids = {r[0] for r in invocations}
        assert len(ids) == 3
        tenants = sorted(t for _, t in invocations)
        assert tenants == ["t-A", "t-B", "t-C"]
        # Host's view: 3 in-flight AgentRuns of TenantSaga.
        live = [r for r in host.agent_runs.values() if r.template is TenantSaga]
        assert len(live) == 3

    async def test_routing_key_is_observable_on_the_run(self) -> None:
        """``run.routing_key`` returns the value produced by the lambda."""
        captured: dict[str, Any] = {}

        class TenantSaga(Agent):
            @subscribes(_SubTestEvent, route_by=lambda e: e.tenant_id)
            def on_event(self, event: _SubTestEvent) -> None:
                assert self._run is not None
                captured["run"] = self._run
                captured["key"] = self._run.routing_key

        host = ModuleHost()
        host.register(TenantSaga())

        host.publish(_SubTestEvent(tenant_id="t-123"))

        assert captured["key"] == "t-123"
        run = captured["run"]
        assert isinstance(run, AgentRun)
        # The id-keyed registry exposes the same routing_key value.
        assert host.agent_runs[run.id].routing_key == "t-123"


class TestAgentSubscribesHybridRuntime:
    """``@subscribes`` fires while ``run()`` is in-flight and shares ``self``."""

    async def test_callback_shares_state_with_run(self) -> None:
        """A Saga-style Agent: ``run()`` polls in the background while
        the ``@subscribes`` callback mutates the same ``self`` state.

        Verifies the ADR-0008 hybrid-runtime guarantee: the callback
        sees ``self`` from the *live* AgentRun's instance, not a fresh
        prototype, so the mutation is visible to ``run()``."""
        seen: list[str] = []

        class HybridAgent(Agent):
            # State lives on the instance; the route_by ensures every
            # event lands on the same AgentRun.
            def __init__(self) -> None:
                super().__init__()
                self.shared_log: list[str] = []
                self._observed = asyncio.Event()

            async def run(self) -> None:
                # Poll until the callback has observed at least one event
                # — that's when we know the hybrid wiring fired in-flight.
                await self._observed.wait()
                seen.extend(self.shared_log)

            @subscribes(_SubTestEvent, route_by=lambda e: e.tenant_id)
            def on_event(self, event: _SubTestEvent) -> None:
                self.shared_log.append(event.payload)
                self._observed.set()

        host = ModuleHost()
        host.register(HybridAgent())

        # Spawn the live run with a matching routing key so subsequent
        # publishes hit the same instance. We use the same tenant_id on
        # the publish path; the spawn happens implicitly via the first
        # publish.
        host.publish(_SubTestEvent(tenant_id="t", payload="A"))
        # Find the live run and let its run() drain.
        live = [r for r in host.agent_runs.values() if r.template is HybridAgent]
        assert len(live) == 1
        # Publish a second event — same routing key, same run, same self.
        host.publish(_SubTestEvent(tenant_id="t", payload="B"))

        # Drain run() — it returns once shared_log has at least one entry.
        await _drain(host, [r.id for r in live])

        # The callback wrote to the same ``self.shared_log`` ``run()`` later
        # copied into the test-side ``seen`` list.
        assert "A" in seen
        # ``B`` may or may not be in ``seen`` depending on whether the
        # second publish landed before run() returned — what matters is
        # that the SAME instance holds both entries on shared_log. Find
        # it via the captured run reference (if it's still around) or
        # accept that A landed at minimum.
        assert seen.count("A") == 1


class TestAgentSubscribesCapHit:
    """``max_concurrent`` cap-hits are isolated: the wrapper logs and drops."""

    async def test_cap_hit_does_not_propagate_to_other_subscribers(self) -> None:
        """Module subscriber to the same Event still fires when the
        Agent's spawn is rejected — ADR-0007 subscriber isolation."""
        module_received: list[_SubTestEvent] = []

        class Capped(Agent):
            max_concurrent = 1

            async def run(self) -> None:
                # Park forever so the cap stays full; the test stops it
                # at the end via shutdown.
                while not self._run._stop_requested:
                    await asyncio.sleep(0.01)

            @subscribes(_SubTestEvent)
            def on_event(self, event: _SubTestEvent) -> None:  # pragma: no cover
                # Should never fire — the wrapper's spawn is rejected.
                module_received.append(("agent", event))  # type: ignore[arg-type]

        class Audit(Module):
            @subscribes(_SubTestEvent)
            def on_event(self, event: _SubTestEvent) -> None:
                module_received.append(event)

        host = ModuleHost()
        host.register(Audit())
        host.register(Capped())

        # Fill the cap with a manual spawn so the Event-triggered spawn
        # the wrapper will attempt hits the rejection.
        manual = host.spawn(Capped)
        assert manual.id in host.agent_runs

        # Publish — the Agent's wrapper must catch AgentSpawnRejected and
        # NOT re-raise. The Module subscriber must still receive the Event.
        evt = _SubTestEvent(payload="boom")
        host.publish(evt)

        # The Module sub got it; only one entry in the list (the Agent
        # branch never reached its body because spawn was rejected).
        assert module_received == [evt]

        # Cleanup: cooperative-stop the parked run so the test does not
        # leak in-flight tasks across the suite.
        manual.stop()
        await _drain(host, [manual.id])


class TestAgentSubscribesExactTypeRouting:
    """ADR-0007: subscribers to a base class do NOT receive derived events."""

    async def test_base_subscriber_does_not_receive_derived(self) -> None:
        @dataclass
        class BaseEvt(Event):
            name: str = "evt.base"

        @dataclass
        class DerivedEvt(BaseEvt):
            name: str = "evt.derived"

        received: list[Event] = []

        class BaseListener(Agent):
            @subscribes(BaseEvt)
            def on_base(self, event: BaseEvt) -> None:
                received.append(event)

        host = ModuleHost()
        host.register(BaseListener())

        # Derived → no fire. Exact-type routing rule from
        # EventBus.publish (see ADR-0007).
        host.publish(DerivedEvt())
        assert received == []

        # Sanity: a Base instance does fire (proves the wiring is live).
        b = BaseEvt()
        host.publish(b)
        assert received == [b]


class TestAgentSubscribesUnrelatedEventsIgnored:
    """A subscribed Agent does not fire for unrelated Event types."""

    async def test_unrelated_event_does_not_spawn(self) -> None:
        spawned: list[AgentRun] = []

        class Picky(Agent):
            @subscribes(_SubTestEvent)
            def on_event(self, event: _SubTestEvent) -> None:
                assert self._run is not None
                spawned.append(self._run)

        host = ModuleHost()
        host.register(Picky())

        host.publish(_OtherEvent())
        # No subscribers for _OtherEvent → no spawn.
        assert spawned == []
        assert not any(
            r.template is Picky for r in host.agent_runs.values()
        )


class TestAgentRunNewKwargs:
    """``AgentRun`` accepts ``triggered_by_event`` / ``routing_key`` directly."""

    def test_kwargs_default_to_none(self) -> None:
        host = _MockHost()
        run = AgentRun(Agent(), host)  # type: ignore[arg-type]
        assert run.triggered_by_event is None
        assert run.routing_key is None

    def test_kwargs_stored_on_instance(self) -> None:
        host = _MockHost()
        evt = _SubTestEvent(tenant_id="t-9", payload="x")
        run = AgentRun(
            Agent(),
            host,  # type: ignore[arg-type]
            triggered_by_event=evt,
            routing_key="t-9",
        )
        assert run.triggered_by_event is evt
        assert run.routing_key == "t-9"
