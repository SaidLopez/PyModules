"""
Tests for :mod:`pymodules.scheduler` and the :func:`pymodules.scheduled`
decorator (issue #13).

Style note: every async test uses a :class:`_MockClock` injected into the
:class:`Scheduler` constructor so the suite runs in <100 ms with no real
``time.sleep`` waits. This mirrors the injected-clock pattern in
``pymodules/resilience/retry.py`` and the ``AgentStateStore`` conformance
suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pymodules import Agent, ModuleHost, scheduled
from pymodules.scheduler import (
    Clock,
    RealClock,
    Schedule,
    Scheduler,
)

# ---------------------------------------------------------------------------
# Mock clock
# ---------------------------------------------------------------------------


class _MockClock:
    """Deterministic :class:`Clock` for tests.

    Time only advances when :meth:`advance` is called from the test. A
    call to :meth:`sleep` blocks (via ``asyncio.Event``) until the test
    has advanced past the requested deadline. This gives the test
    fine-grained control: "fire exactly N times when I advance by
    N intervals" is a deterministic assertion, not a sleep-and-hope.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now: datetime = start or datetime(2026, 1, 1, tzinfo=UTC)
        # List of (deadline, asyncio.Event) tuples — sleepers waiting for
        # the clock to reach a particular point.
        self._sleepers: list[tuple[datetime, asyncio.Event]] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        deadline = self._now + timedelta(seconds=seconds)
        ev = asyncio.Event()
        self._sleepers.append((deadline, ev))
        # If the clock is already past the deadline, fire immediately.
        if self._now >= deadline:
            ev.set()
        await ev.wait()

    async def advance(self, seconds: float) -> None:
        """Jump simulated time forward by ``seconds``.

        Wakes any sleeper whose deadline is now in the past, then yields
        to the event loop so the woken tasks make progress. After each
        yield we re-check the sleeper list: a newly-fired scheduler tick
        immediately re-enters ``sleep`` and may register a *new*
        sleeper with a deadline already in the past (the catch-up
        case), which we must wake on this same ``advance`` call to
        deliver all the missed ticks in one shot.

        The pump yields ``asyncio.sleep(0)`` several times per
        outer iteration because each scheduler tick chains through
        several awaits (sleep → create_task → invoke yield → recheck →
        next sleep), and we need each chain step to advance before we
        can check whether a new sleeper landed at a deadline in the
        past.
        """
        self._now = self._now + timedelta(seconds=seconds)
        # Pump-and-wake loop: keep waking due sleepers and yielding to
        # the loop until no new sleepers come due. Bounded to avoid
        # an unbounded spin on a misbehaving scheduler.
        for _ in range(200):
            woke_any = False
            remaining: list[tuple[datetime, asyncio.Event]] = []
            for deadline, ev in self._sleepers:
                if deadline <= self._now:
                    ev.set()
                    woke_any = True
                else:
                    remaining.append((deadline, ev))
            self._sleepers = remaining
            # Pump several times to let each scheduler tick chain
            # through its full step sequence before we re-check the
            # sleeper list.
            for _ in range(10):
                await asyncio.sleep(0)
            if not woke_any:
                # No sleepers woke this round. One more drain pass to
                # let any still-running task settle before returning.
                for _ in range(5):
                    await asyncio.sleep(0)
                # Final check: did anything land in the sleeper list
                # at a deadline already in the past during the drain?
                still_due = any(d <= self._now for d, _ in self._sleepers)
                if not still_due:
                    return


# ---------------------------------------------------------------------------
# Decorator unit tests
# ---------------------------------------------------------------------------


class TestScheduledDecorator:
    """``@scheduled`` requires exactly one of interval/cron and validates eagerly."""

    def test_interval_stores_schedule_on_function(self) -> None:
        @scheduled(interval=timedelta(seconds=5))
        def tick(self):  # noqa: ANN001 — Agent method shape
            pass

        from pymodules.agent import SCHEDULED_ATTR

        sched = getattr(tick, SCHEDULED_ATTR)
        assert isinstance(sched, Schedule)
        assert sched.interval == timedelta(seconds=5)
        assert sched.cron is None

    def test_cron_stores_schedule_on_function(self) -> None:
        @scheduled(cron="*/15 * * * *")
        def tick(self):  # noqa: ANN001
            pass

        from pymodules.agent import SCHEDULED_ATTR

        sched = getattr(tick, SCHEDULED_ATTR)
        assert sched.cron == "*/15 * * * *"
        assert sched.interval is None

    def test_neither_raises(self) -> None:
        with pytest.raises(TypeError, match="exactly one of"):

            @scheduled()  # type: ignore[call-overload]
            def tick(self):  # noqa: ANN001
                pass

    def test_both_raises(self) -> None:
        with pytest.raises(TypeError, match="exactly one of"):

            @scheduled(interval=timedelta(seconds=5), cron="* * * * *")
            def tick(self):  # noqa: ANN001
                pass

    def test_invalid_cron_raises_at_decoration_time(self) -> None:
        with pytest.raises(ValueError):

            @scheduled(cron="not a cron expression")
            def tick(self):  # noqa: ANN001
                pass

    def test_zero_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):

            @scheduled(interval=timedelta(seconds=0))
            def tick(self):  # noqa: ANN001
                pass


# ---------------------------------------------------------------------------
# Cron parser unit tests (the homebrew 5-field parser)
# ---------------------------------------------------------------------------


class TestCronParser:
    """Sanity-check the 5-field cron parser."""

    def test_star_fires_every_minute(self) -> None:
        sch = Schedule(cron="* * * * *")
        base = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
        nxt = sch.next_fire_after(base)
        # Next minute boundary strictly after :00:30 → :01:00.
        assert nxt == datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC)

    def test_hourly_on_the_hour(self) -> None:
        sch = Schedule(cron="0 * * * *")
        base = datetime(2026, 1, 1, 12, 30, 0, tzinfo=UTC)
        nxt = sch.next_fire_after(base)
        # Next minute-0 strictly after 12:30 → 13:00.
        assert nxt == datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)

    def test_step_value(self) -> None:
        sch = Schedule(cron="*/15 * * * *")
        base = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
        nxt = sch.next_fire_after(base)
        # Minute set is {0,15,30,45} — next strictly after :05 → :15.
        assert nxt.minute == 15

    def test_range(self) -> None:
        sch = Schedule(cron="0 9-17 * * *")
        base = datetime(2026, 1, 1, 8, 30, 0, tzinfo=UTC)
        # Next 9-17 hour at minute 0 → 09:00.
        nxt = sch.next_fire_after(base)
        assert nxt.hour == 9 and nxt.minute == 0

    def test_list(self) -> None:
        sch = Schedule(cron="0,30 * * * *")
        base = datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC)
        nxt = sch.next_fire_after(base)
        # Next of {:00,:30} strictly after :10 → :30.
        assert nxt.minute == 30


# ---------------------------------------------------------------------------
# Scheduler with mock clock — interval firing
# ---------------------------------------------------------------------------


class TestSchedulerIntervalFiring:
    """``@scheduled(interval=...)`` fires every N simulated seconds."""

    async def test_fires_every_5_simulated_seconds(self) -> None:
        clock = _MockClock()
        sched = Scheduler(clock=clock)

        call_count = 0

        async def method() -> None:
            nonlocal call_count
            call_count += 1

        sched.add("run-1", method, Schedule(interval=timedelta(seconds=5)))
        sched.start()
        # Loop registers its first sleeper after a yield.
        await asyncio.sleep(0)

        # Advance by 5s → expect 1 fire.
        await clock.advance(5.0)
        assert call_count == 1

        # Advance by another 5s → expect 2 fires total.
        await clock.advance(5.0)
        assert call_count == 2

        # Advance by 15s → expect 3 more fires (total 5).
        await clock.advance(15.0)
        assert call_count == 5

        sched.stop()

    async def test_sync_method_is_supported(self) -> None:
        """``@scheduled`` may decorate a plain ``def`` method, not only async."""
        clock = _MockClock()
        sched = Scheduler(clock=clock)
        call_count = 0

        def method() -> None:
            nonlocal call_count
            call_count += 1

        sched.add("run-1", method, Schedule(interval=timedelta(seconds=1)))
        sched.start()
        await asyncio.sleep(0)
        await clock.advance(1.0)
        assert call_count == 1
        sched.stop()


# ---------------------------------------------------------------------------
# Scheduler with mock clock — cron firing
# ---------------------------------------------------------------------------


class TestSchedulerCronFiring:
    """``@scheduled(cron=...)`` fires at the right simulated minute."""

    async def test_hourly_cron(self) -> None:
        # Start at 12:00:00 on the dot so the first fire is exactly
        # 60 minutes later (13:00:00).
        clock = _MockClock(start=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        sched = Scheduler(clock=clock)
        call_count = 0

        async def method() -> None:
            nonlocal call_count
            call_count += 1

        sched.add("run-1", method, Schedule(cron="0 * * * *"))
        sched.start()
        await asyncio.sleep(0)

        # Advance by 30 minutes — no fire yet (next is at 13:00).
        await clock.advance(30 * 60)
        assert call_count == 0

        # Advance by another 30 minutes — now we cross 13:00.
        await clock.advance(30 * 60)
        assert call_count == 1

        sched.stop()


# ---------------------------------------------------------------------------
# Scheduler with mock clock — multiple methods on one template
# ---------------------------------------------------------------------------


class TestSchedulerMultipleMethods:
    """Multiple ``@scheduled`` methods on one template each fire on their own schedule."""

    async def test_two_intervals_on_one_run(self) -> None:
        clock = _MockClock()
        sched = Scheduler(clock=clock)
        fast_count = 0
        slow_count = 0

        async def fast() -> None:
            nonlocal fast_count
            fast_count += 1

        async def slow() -> None:
            nonlocal slow_count
            slow_count += 1

        sched.add("run-1", fast, Schedule(interval=timedelta(seconds=1)))
        sched.add("run-1", slow, Schedule(interval=timedelta(seconds=5)))
        sched.start()
        await asyncio.sleep(0)

        # Advance 5s in one shot. Each method's loop schedules one
        # sleeper at registration → 5s in, fast has fired once at
        # 1s, then re-slept 1s (deadline 2s), and so on. Because
        # ``advance`` pumps the loop multiple times, every tick that
        # falls within the new clock window fires.
        await clock.advance(5.0)
        # Fast: ticks at 1,2,3,4,5 → 5 fires.
        # Slow: ticks at 5 → 1 fire.
        assert fast_count == 5
        assert slow_count == 1

        sched.stop()


# ---------------------------------------------------------------------------
# Re-entry behavior — skip
# ---------------------------------------------------------------------------


class TestSchedulerReentrySkip:
    """If a previous invocation is still in flight, the next tick is **skipped**."""

    async def test_long_running_method_skips_overlapping_ticks(self) -> None:
        clock = _MockClock()
        sched = Scheduler(clock=clock)

        invocations = 0
        # ``release`` lets the test pin the method "in flight" until the
        # test explicitly drains it.
        release = asyncio.Event()

        async def slow_method() -> None:
            nonlocal invocations
            invocations += 1
            await release.wait()

        sched.add("run-1", slow_method, Schedule(interval=timedelta(seconds=5)))
        sched.start()
        await asyncio.sleep(0)

        # First fire at +5s — method begins running, awaits ``release``.
        await clock.advance(5.0)
        assert invocations == 1

        # Advance by another 5s while the first invocation is still
        # blocked. The re-entry guard must SKIP this tick — invocations
        # stays at 1.
        await clock.advance(5.0)
        assert invocations == 1, (
            "Re-entry guard violated: a second invocation queued while "
            "the first was still in flight"
        )

        # Release the first invocation; the next tick should proceed.
        release.set()
        # Drain the in-flight method.
        for _ in range(10):
            await asyncio.sleep(0)

        # Now advance another 5s — fires again.
        release.clear()
        release.set()  # keep release set so subsequent ticks complete
        await clock.advance(5.0)
        # Total: 1 (original) + 1 (new) = 2 (the skipped one stays
        # skipped — not queued).
        assert invocations == 2

        sched.stop()


# ---------------------------------------------------------------------------
# Host integration — lazy construction
# ---------------------------------------------------------------------------


class TestHostLazyScheduler:
    """A host with no scheduled Agents must not construct a :class:`Scheduler`."""

    def test_no_agents_no_scheduler(self) -> None:
        host = ModuleHost()
        assert host.scheduler is None

    def test_agent_with_no_scheduled_methods_no_scheduler(self) -> None:
        class PlainAgent(Agent):
            async def run(self) -> None:
                pass

        host = ModuleHost()
        host.register(PlainAgent())
        assert host.scheduler is None

    def test_agent_with_scheduled_method_constructs_scheduler(self) -> None:
        class TickerAgent(Agent):
            @scheduled(interval=timedelta(seconds=10))
            async def tick(self) -> None:
                pass

        host = ModuleHost()
        assert host.scheduler is None
        host.register(TickerAgent())
        assert host.scheduler is not None
        # Second registration of another scheduled template returns the
        # same scheduler instance.
        scheduler_id = id(host.scheduler)

        class OtherAgent(Agent):
            @scheduled(interval=timedelta(seconds=60))
            async def other(self) -> None:
                pass

        host.register(OtherAgent())
        assert id(host.scheduler) == scheduler_id


# ---------------------------------------------------------------------------
# Host integration — full end-to-end with mock clock
# ---------------------------------------------------------------------------


class TestHostScheduledAgent:
    """End-to-end: register an Agent with ``@scheduled``, spawn, observe ticks."""

    async def test_scheduled_method_fires_via_host(self) -> None:
        ticks = 0

        class TickerAgent(Agent):
            @scheduled(interval=timedelta(seconds=1))
            async def on_tick(self) -> None:
                nonlocal ticks
                ticks += 1

        host = ModuleHost()
        host.register(TickerAgent())
        # Replace the real clock with a mock one BEFORE spawn so the
        # scheduled loops are driven by simulated time. The host
        # constructs the scheduler at ``register`` time; we reach in to
        # swap the clock. This is the documented test seam — the
        # public surface remains the constructor-injected clock used
        # by ``Scheduler`` directly.
        mock = _MockClock()
        host._scheduler._clock = mock

        host.spawn(TickerAgent)
        # Let the spawned task settle and the scheduler loops register
        # their first sleeper.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Advance 3 simulated seconds → expect 3 ticks.
        await mock.advance(3.0)
        assert ticks == 3

        # Stop the run cooperatively. The scheduler's
        # ``set_run_alive_predicate`` reports the run as gone the
        # moment :class:`_run_agent` removes it from ``agent_runs``;
        # since this Agent has no ``async def run()``, that path is
        # never traversed and the predicate would keep firing. We
        # explicitly call ``host._scheduler.stop()`` to model the
        # host-shutdown integration #11 will land. Once #11 lands its
        # ``shutdown()`` change, that call will be unnecessary.
        host._scheduler.stop()

    async def test_scheduler_stops_when_explicitly_stopped(self) -> None:
        """After :meth:`Scheduler.stop` no further ticks fire, even on time-jump.

        Models the host-shutdown contract directly: ``host.shutdown()``
        calls ``scheduler.stop()`` (ticket #11 wired this in), and after
        that no scheduled methods fire regardless of clock advances.
        """
        ticks = 0

        class TickerAgent(Agent):
            @scheduled(interval=timedelta(seconds=1))
            async def on_tick(self) -> None:
                nonlocal ticks
                ticks += 1

        host = ModuleHost()
        host.register(TickerAgent())
        mock = _MockClock()
        host._scheduler._clock = mock

        host.spawn(TickerAgent)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await mock.advance(2.0)
        assert ticks == 2
        baseline = ticks

        # Stop the scheduler.
        host._scheduler.stop()
        # Give cancelled tasks a chance to wind down.
        for _ in range(10):
            await asyncio.sleep(0)

        # Advance further simulated time — no further ticks should fire.
        await mock.advance(10.0)
        assert ticks == baseline

    async def test_scheduler_stops_via_host_shutdown(self) -> None:
        """``host.shutdown()`` stops the scheduler (ticket #11 wiring).

        Ticket #11 added scheduler-stop to :meth:`ModuleHost.shutdown`
        — exercise that integration end-to-end so we'd notice if a
        future refactor dropped the call.
        """
        ticks = 0

        class TickerAgent(Agent):
            @scheduled(interval=timedelta(seconds=1))
            async def on_tick(self) -> None:
                nonlocal ticks
                ticks += 1

        host = ModuleHost()
        host.register(TickerAgent())
        mock = _MockClock()
        host._scheduler._clock = mock

        host.spawn(TickerAgent)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await mock.advance(2.0)
        assert ticks >= 1

        # The synchronous ``shutdown()`` must not block on grace because
        # this is a no-``run()`` Agent and shutdown's per-run wait won't
        # apply. Call shutdown() and assert the scheduler is no longer
        # running.
        host.shutdown(wait=False)
        assert host._scheduler.running is False

        # Settle and advance the clock — no further ticks.
        for _ in range(10):
            await asyncio.sleep(0)
        baseline = ticks
        await mock.advance(10.0)
        assert ticks == baseline


# ---------------------------------------------------------------------------
# RealClock smoke test (sanity, no timing assertions)
# ---------------------------------------------------------------------------


class TestRealClock:
    def test_real_clock_now_is_utc(self) -> None:
        clock = RealClock()
        n = clock.now()
        assert n.tzinfo is not None

    def test_real_clock_satisfies_protocol(self) -> None:
        # ``Clock`` is ``runtime_checkable`` — must accept RealClock.
        assert isinstance(RealClock(), Clock)

    async def test_real_clock_zero_sleep_is_no_op(self) -> None:
        clock = RealClock()
        # Should return roughly immediately. We don't assert on timing
        # to avoid flakiness on slow CI; just that it completes.
        await clock.sleep(0.0)
