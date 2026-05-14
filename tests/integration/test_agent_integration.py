"""
End-to-end integration tests for the :class:`Agent` / :class:`AgentRun`
primitive against a real :class:`ModuleHost`.

Covers the foundation-slice acceptance criteria of ticket #10:

- ``register(Agent())`` stores a template; ``spawn(Template)`` produces an
  ``AgentRun`` whose ``run()`` coroutine actually runs.
- An AgentRun appears in ``host.agent_runs`` while live and disappears
  on natural termination.
- ``run.stop()`` is honoured cooperatively by ``run()``.
- ``host.spawn(UnregisteredTemplate)`` raises :class:`AgentNotRegistered`.
- An Agent's ``self._host.dispatch_async(SomeCommand)`` flows through the
  configured middleware chain exactly once.
- An Agent's ``self._host.publish(SomeEvent)`` reaches a Module's
  ``@subscribes`` handler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from pymodules import (
    Agent,
    AgentFailed,
    AgentNotRegistered,
    AgentRunStuck,
    AgentSpawnRejected,
    Command,
    CommandRequest,
    CommandResponse,
    Event,
    Module,
    ModuleHost,
    ModuleHostConfig,
    RetryPolicy,
    handles,
    subscribes,
)

# ---------------------------------------------------------------------------
# Test fixtures: a tiny Command + Event the Agent will dispatch / publish
# ---------------------------------------------------------------------------


@dataclass
class PingRequest(CommandRequest):
    note: str = ""


@dataclass
class PingResponse(CommandResponse):
    seen: str = ""


class PingCommand(Command[PingRequest, PingResponse]):
    name = "agent.ping"


@dataclass
class AgentTickEvent(Event):
    """Event the test Agent publishes to verify EventBus delivery."""

    seq: int = 0
    name: str = "agent.tick"


# ---------------------------------------------------------------------------
# Lifecycle: spawn → run → terminate
# ---------------------------------------------------------------------------


class TestAgentSpawnLifecycle:
    """``host.spawn`` produces a live AgentRun whose ``run()`` executes."""

    async def test_spawn_runs_agent_and_returns_naturally(self) -> None:
        """A trivial ``run()`` that returns immediately leaves the
        registry empty afterwards."""

        ran = asyncio.Event()

        class OneShot(Agent):
            async def run(self) -> None:
                ran.set()

        host = ModuleHost()
        host.register(OneShot())

        run = host.spawn(OneShot)
        # The AgentRun is in the registry the moment spawn returns.
        assert run.id in host.agent_runs
        assert host.agent_runs[run.id] is run

        # Let the scheduled task run to completion.
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        # Yield once so the host's _run_agent finally-block clears the
        # registry entry. Without this we'd race the awaiter against
        # the cleanup callback.
        for _ in range(5):
            if run.id not in host.agent_runs:
                break
            await asyncio.sleep(0)

        assert run.id not in host.agent_runs

    async def test_agent_runs_is_read_only_mapping(self) -> None:
        """``host.agent_runs`` exposes a ``MappingProxyType`` view —
        callers cannot mutate it directly."""

        class Idle(Agent):
            async def run(self) -> None:
                await asyncio.sleep(0)

        host = ModuleHost()
        host.register(Idle())
        run = host.spawn(Idle)

        # Mutating the proxy raises TypeError.
        with pytest.raises(TypeError):
            host.agent_runs["fake"] = run  # type: ignore[index]

        # Drain
        for _ in range(5):
            if run.id not in host.agent_runs:
                break
            await asyncio.sleep(0)

    async def test_spawn_unregistered_template_raises(self) -> None:
        """``host.spawn(SomethingNotRegistered)`` raises ``AgentNotRegistered``."""

        class NotRegistered(Agent):
            async def run(self) -> None:
                return None

        host = ModuleHost()
        with pytest.raises(AgentNotRegistered) as exc_info:
            host.spawn(NotRegistered)
        assert exc_info.value.template is NotRegistered

    async def test_agent_without_run_spawns_without_task(self) -> None:
        """A pure-callback Agent (no ``run()``) is registerable and
        spawnable; nothing schedules and nothing terminates.

        Pure-callback Agents are alive between triggers (ADR-0008
        default lifetime). In this foundation slice no triggers exist
        yet, so the AgentRun sits in the registry until the test
        explicitly stops it; we just assert spawn succeeds.
        """

        class CallbackOnly(Agent):
            pass

        host = ModuleHost()
        host.register(CallbackOnly())
        run = host.spawn(CallbackOnly)

        # The AgentRun is alive in the registry (no ``run()`` to return).
        assert run.id in host.agent_runs


# ---------------------------------------------------------------------------
# Cooperative stop
# ---------------------------------------------------------------------------


class TestAgentCooperativeStop:
    """``run.stop()`` sets ``_stop_requested`` and the loop honours it."""

    async def test_stop_terminates_a_polling_run(self) -> None:
        ticks = 0

        class PollingAgent(Agent):
            async def run(self) -> None:
                nonlocal ticks
                # Cooperative checkpoint pattern straight from the
                # Agent docstring.
                while not self._run._stop_requested:
                    ticks += 1
                    await asyncio.sleep(0)

        host = ModuleHost()
        host.register(PollingAgent())
        run = host.spawn(PollingAgent)

        # Let it spin a few iterations.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert ticks >= 1

        run.stop()

        # Drain until the registry is cleared (cooperative stop +
        # finally-block cleanup).
        for _ in range(50):
            if run.id not in host.agent_runs:
                break
            await asyncio.sleep(0)

        assert run.id not in host.agent_runs
        assert run._stop_requested is True


# ---------------------------------------------------------------------------
# Dispatch flows through the host's middleware chain exactly once
# ---------------------------------------------------------------------------


class TestAgentDispatchThroughChain:
    """An Agent's ``self._host.dispatch_async`` runs the chain once."""

    async def test_dispatch_increments_recording_middleware_once(self) -> None:
        invocations: list[str] = []

        async def recorder(command, next_call):  # type: ignore[no-untyped-def]
            invocations.append(command.name)
            return await next_call(command)

        class PingModule(Module):
            @handles(PingCommand)
            async def on_ping(self, command: PingCommand) -> PingResponse:
                assert command.request is not None
                return PingResponse(seen=command.request.note)

        result: dict[str, PingResponse | None] = {"resp": None}

        class DispatcherAgent(Agent):
            async def run(self) -> None:
                # Single dispatch through the host chain. Done from an
                # Agent task — fresh top-level entry, not chain re-entry.
                assert self._host is not None
                result["resp"] = await self._host.dispatch_async(
                    PingCommand(request=PingRequest(note="hi"))
                )

        host = ModuleHost(config=ModuleHostConfig(middleware=[recorder]))
        host.register(PingModule())
        host.register(DispatcherAgent())

        run = host.spawn(DispatcherAgent)

        # Drain.
        for _ in range(50):
            if run.id not in host.agent_runs:
                break
            await asyncio.sleep(0)

        assert result["resp"] is not None
        assert result["resp"].seen == "hi"
        # Exactly one chain traversal — verifies ADR-0008's "no per-Agent
        # chain" guarantee: the configured middleware sees the dispatch
        # *once*, not once per layer.
        assert invocations == ["agent.ping"]


# ---------------------------------------------------------------------------
# Publish reaches a separate Module's @subscribes handler
# ---------------------------------------------------------------------------


class TestAgentPublishReachesSubscriber:
    """An Agent's ``self._host.publish`` is delivered to subscribing Modules."""

    async def test_publish_from_agent_reaches_module_subscriber(self) -> None:
        received: list[AgentTickEvent] = []

        class AuditModule(Module):
            @subscribes(AgentTickEvent)
            def on_tick(self, event: AgentTickEvent) -> None:
                received.append(event)

        class PublisherAgent(Agent):
            async def run(self) -> None:
                assert self._host is not None
                self._host.publish(AgentTickEvent(seq=1))
                self._host.publish(AgentTickEvent(seq=2))

        host = ModuleHost()
        host.register(AuditModule())
        host.register(PublisherAgent())

        run = host.spawn(PublisherAgent)

        for _ in range(50):
            if run.id not in host.agent_runs:
                break
            await asyncio.sleep(0)

        assert [e.seq for e in received] == [1, 2]


# ---------------------------------------------------------------------------
# Ticket #11 — AgentFailed Event + restart_policy + max_concurrent + shutdown_grace
# ---------------------------------------------------------------------------


async def _drain_until_gone(host: ModuleHost, run_id: str, ticks: int = 200) -> None:
    """Yield to the loop until ``run_id`` disappears from ``host.agent_runs``.

    Helper used by the AgentFailed / restart tests below; mirrors the
    drain pattern from the lifecycle tests but consolidated so each
    test reads cleanly.
    """
    for _ in range(ticks):
        if run_id not in host.agent_runs:
            return
        await asyncio.sleep(0)


class TestAgentFailedOnUnhandledException:
    """An unhandled exception in ``run()`` publishes :class:`AgentFailed`."""

    async def test_unhandled_exception_publishes_agent_failed(self) -> None:
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        class BoomAgent(Agent):
            async def run(self) -> None:
                raise RuntimeError("kaboom")

        host = ModuleHost()
        host.register(FailureAuditor())
        host.register(BoomAgent())

        run = host.spawn(BoomAgent)
        await _drain_until_gone(host, run.id)

        # Exactly one AgentFailed for this run, carrying the exception.
        assert len(failures) == 1
        event = failures[0]
        assert event.agent_run_id == run.id
        assert event.agent_template_name == "BoomAgent"
        assert isinstance(event.error, RuntimeError)
        assert str(event.error) == "kaboom"
        # The crashed AgentRun is gone from the registry.
        assert run.id not in host.agent_runs

    async def test_cooperative_stop_does_not_publish_agent_failed(self) -> None:
        """An Agent that returns normally after ``stop()`` is *not* a failure."""
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        class PollingAgent(Agent):
            async def run(self) -> None:
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        host = ModuleHost()
        host.register(FailureAuditor())
        host.register(PollingAgent())

        run = host.spawn(PollingAgent)
        await asyncio.sleep(0)
        run.stop()
        await _drain_until_gone(host, run.id)

        assert failures == []


class TestAgentRestartPolicy:
    """``restart_policy = RetryPolicy(max_retries=N)`` drives N re-spawns."""

    async def test_restart_policy_respawns_until_exhaustion(self) -> None:
        """With ``max_retries=2``, a crashing Agent produces 3 AgentFailed
        events (1 initial crash + 2 restart-attempt crashes) and then no
        further restarts."""
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        # ``base_delay=0`` plus a zero exponential effectively eliminates
        # the backoff in tests. ``RetryPolicy.calculate_delay`` returns
        # ``base_delay * exponential_base**attempt`` so ``base_delay=0``
        # produces a zero delay regardless of attempt.
        class CrashAgent(Agent):
            restart_policy = RetryPolicy(max_retries=2, base_delay=0.0)

            async def run(self) -> None:
                raise RuntimeError("crash")

        host = ModuleHost()
        host.register(FailureAuditor())
        host.register(CrashAgent())

        run = host.spawn(CrashAgent)
        # Drain across the whole restart chain — each crash spawns a new
        # run via ``host.spawn`` from inside ``_run_agent``, and during
        # the restart's backoff sleep the ``agent_runs`` registry can be
        # transiently empty. We watch the AgentFailed count instead;
        # once 3 events arrived (1 initial + 2 restart-attempt crashes)
        # the chain is finished by construction (should_retry returns
        # False at attempt=2 with max_retries=2).
        for _ in range(400):
            if len(failures) >= 3 and not host.agent_runs:
                break
            await asyncio.sleep(0)

        # 1 original + 2 restart-attempt crashes.
        assert len(failures) == 3, (
            f"Expected 3 AgentFailed events (initial crash + 2 restarts), got {len(failures)}"
        )
        # Every event carries the original exception type.
        for event in failures:
            assert isinstance(event.error, RuntimeError)
            assert event.agent_template_name == "CrashAgent"
        # All ids are distinct — each crash was a separate AgentRun.
        ids = {event.agent_run_id for event in failures}
        assert len(ids) == 3
        # The originally-spawned run id is the first one published.
        assert failures[0].agent_run_id == run.id

    async def test_restart_policy_zero_retries_publishes_one_failure(self) -> None:
        """``max_retries=0`` means: publish one AgentFailed, then no
        restart. Equivalent to ``restart_policy=None`` from the visible
        side, but exercises the should_retry-False branch."""
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        class CrashAgent(Agent):
            restart_policy = RetryPolicy(max_retries=0, base_delay=0.0)

            async def run(self) -> None:
                raise RuntimeError("one-shot")

        host = ModuleHost()
        host.register(FailureAuditor())
        host.register(CrashAgent())

        host.spawn(CrashAgent)
        for _ in range(50):
            if not host.agent_runs:
                break
            await asyncio.sleep(0)

        assert len(failures) == 1


class TestMaxConcurrentCap:
    """``max_concurrent=N`` caps in-flight runs of that template."""

    async def test_third_spawn_raises_when_cap_is_two(self) -> None:
        class LongRunner(Agent):
            max_concurrent = 2

            async def run(self) -> None:
                # Stay alive cooperatively until stopped.
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        host = ModuleHost()
        host.register(LongRunner())

        run1 = host.spawn(LongRunner)
        run2 = host.spawn(LongRunner)
        # Both alive.
        assert run1.id in host.agent_runs
        assert run2.id in host.agent_runs

        # 3rd spawn is rejected — no queueing.
        with pytest.raises(AgentSpawnRejected) as exc_info:
            host.spawn(LongRunner)
        # Error message names the cap and the template.
        assert "max_concurrent=2" in str(exc_info.value)
        assert "LongRunner" in str(exc_info.value)

        # The two existing runs are unaffected by the failed spawn.
        assert run1.id in host.agent_runs
        assert run2.id in host.agent_runs

        # Drain.
        run1.stop()
        run2.stop()
        for _ in range(50):
            if not host.agent_runs:
                break
            await asyncio.sleep(0)

    async def test_spawn_succeeds_again_after_a_run_terminates(self) -> None:
        class LongRunner(Agent):
            max_concurrent = 2

            async def run(self) -> None:
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        host = ModuleHost()
        host.register(LongRunner())

        run1 = host.spawn(LongRunner)
        run2 = host.spawn(LongRunner)

        with pytest.raises(AgentSpawnRejected):
            host.spawn(LongRunner)

        # Terminate one — the slot opens up.
        run1.stop()
        await _drain_until_gone(host, run1.id)

        # 3rd spawn now succeeds.
        run3 = host.spawn(LongRunner)
        assert run3.id in host.agent_runs
        assert run2.id in host.agent_runs

        # Drain.
        run2.stop()
        run3.stop()
        for _ in range(50):
            if not host.agent_runs:
                break
            await asyncio.sleep(0)

    async def test_cap_is_per_template_not_shared(self) -> None:
        """Two different templates with ``max_concurrent=1`` each don't
        share the cap — registering one's run does not block the other."""

        class A(Agent):
            max_concurrent = 1

            async def run(self) -> None:
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        class B(Agent):
            max_concurrent = 1

            async def run(self) -> None:
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        host = ModuleHost()
        host.register(A())
        host.register(B())

        run_a = host.spawn(A)
        run_b = host.spawn(B)
        assert run_a.id in host.agent_runs
        assert run_b.id in host.agent_runs

        # Each cap is independent.
        with pytest.raises(AgentSpawnRejected):
            host.spawn(A)
        with pytest.raises(AgentSpawnRejected):
            host.spawn(B)

        run_a.stop()
        run_b.stop()
        for _ in range(50):
            if not host.agent_runs:
                break
            await asyncio.sleep(0)


class TestShutdownGrace:
    """``shutdown_grace`` controls cooperative-stop vs hard-cancel."""

    async def test_cooperative_path_no_agent_failed(self) -> None:
        """An Agent that honours ``_stop_requested`` terminates within
        the grace period; no AgentFailed is published."""
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        class CooperativeAgent(Agent):
            async def run(self) -> None:
                while not self._run._stop_requested:
                    await asyncio.sleep(0)

        host = ModuleHost(config=ModuleHostConfig(shutdown_grace=1.0))
        host.register(FailureAuditor())
        host.register(CooperativeAgent())

        run = host.spawn(CooperativeAgent)
        await asyncio.sleep(0)
        assert run.id in host.agent_runs

        # ``shutdown`` itself drives the cooperative-stop wait via a
        # fresh ``asyncio.run`` in a background thread (the calling
        # thread already holds an event loop — this async test).
        # Bridge synchronously by running shutdown in the default
        # executor so the host's internal ``asyncio.run`` is not nested.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, host.shutdown)

        # Cooperative termination — no AgentFailed.
        assert failures == []
        # Registry drained.
        assert run.id not in host.agent_runs

    async def test_hard_cancel_publishes_agent_run_stuck(self) -> None:
        """An Agent that ignores ``_stop_requested`` past the grace is
        hard-cancelled, and the host publishes :class:`AgentFailed`
        carrying :class:`AgentRunStuck`."""
        failures: list[AgentFailed] = []

        class FailureAuditor(Module):
            @subscribes(AgentFailed)
            def on_failed(self, event: AgentFailed) -> None:
                failures.append(event)

        class StubbornAgent(Agent):
            async def run(self) -> None:
                # Sleep through the grace period, deliberately ignoring
                # ``_stop_requested``. Use a long sleep so the
                # hard-cancel is what terminates the task.
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    # Re-raise so the cancellation actually unwinds.
                    raise

        host = ModuleHost(config=ModuleHostConfig(shutdown_grace=0.05))
        host.register(FailureAuditor())
        host.register(StubbornAgent())

        run = host.spawn(StubbornAgent)
        await asyncio.sleep(0)
        assert run.id in host.agent_runs

        # Run shutdown in the executor so the host's internal
        # ``asyncio.run`` does not nest inside our test's running loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, host.shutdown)

        # Exactly one AgentFailed carrying AgentRunStuck.
        stuck_events = [f for f in failures if isinstance(f.error, AgentRunStuck)]
        assert len(stuck_events) == 1
        stuck = stuck_events[0]
        assert stuck.agent_template_name == "StubbornAgent"
        assert stuck.agent_run_id == run.id
        assert "did not honour stop" in str(stuck.error)
