"""
Internal scheduler for ``@scheduled`` Agent methods (ADR-0008 / issue #13).

This module is the in-process scheduler that backs the ``@scheduled``
decorator on :class:`pymodules.Agent` methods. It is deliberately
*internal* to the framework in v1 (not part of the public surface), but
separable for testing — the :class:`Clock` Protocol lets unit tests
substitute a mock clock and advance simulated time without ``time.sleep``.

Two schedule shapes are supported:

- :class:`Schedule` with ``interval=timedelta(...)`` — fire every N
  seconds; no drift correction (each fire is scheduled relative to the
  previous fire's *target* time).
- :class:`Schedule` with ``cron="m h dom mon dow"`` — standard 5-field
  cron expression. The parser supports ``*``, integers, ``a-b`` ranges,
  ``a,b,c`` lists, and ``*/step`` (or ``a-b/step``) step values.

**Re-entry behavior (pinned).** When the previous invocation of a
scheduled method is *still running* at its next fire time, the
scheduler **skips** that tick and logs a warning. The alternatives —
queueing missed ticks or firing in parallel — were rejected because:

- *Queueing* lets a slow method silently build up an arbitrarily long
  backlog, then storm the loop once it finally drains. This is the
  classic failure mode of cron-driven systems that mistakenly assume
  "every minute" means "exactly N invocations per minute".
- *Parallel* invocations on the same Agent body break the per-instance
  state assumption the rest of ADR-0008 leans on (one AgentRun, one
  Python coroutine at a time).

Skip is the conservative default that keeps the system steady; users
who want catch-up semantics can pin their own pattern on top of the
primitive (e.g., set the method to do a self-recovery sweep on each
tick).

Design notes for code review:

- The Clock Protocol mirrors the style of
  :class:`pymodules.agent_state.AgentStateStore` and
  :class:`pymodules.resilience.idempotency.IdempotencyStore`: small,
  ``runtime_checkable``, with a bundled production default. Tests
  substitute via constructor injection, not monkeypatching.
- :class:`Scheduler` owns one ``asyncio.Task`` per registered
  ``(run_id, method)`` triple. The loop body waits on
  ``clock.sleep(...)``, then invokes the method. Stopping is
  cooperative: :meth:`Scheduler.stop` flips ``_running = False`` and
  cancels the live tasks.
- The cron parser is intentionally a tiny homebrew (no ``croniter``
  dependency) — five-field, minute resolution, no seconds, no weekday
  shorthand. If users ask for more, ``croniter`` becomes a contrib
  extra rather than a core dep.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .logging import get_logger

UTC = timezone.utc

if TYPE_CHECKING:
    pass

scheduler_logger = get_logger("scheduler")


# ---------------------------------------------------------------------------
# Clock Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Time source for the :class:`Scheduler`.

    Production code uses :class:`RealClock`; tests substitute a mock
    clock (see ``tests/test_scheduler.py``) to advance simulated time
    without real ``asyncio.sleep`` waits.

    Contract:

    - :meth:`now` returns the current time as a ``datetime`` in UTC.
      Implementations must be consistent — two consecutive ``now()``
      calls must not go backwards.
    - :meth:`sleep` is an async pause of *at least* ``seconds`` seconds
      of clock time. Mock implementations may complete in zero
      wall-clock time but must still advance their internal ``now()``.
    """

    def now(self) -> datetime:
        """Current time in UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Async pause for ``seconds`` clock-seconds."""
        ...


class RealClock:
    """Production :class:`Clock` backed by ``datetime.now(UTC)`` + ``asyncio.sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        # ``asyncio.sleep`` is the right primitive even for ``seconds <= 0``
        # — it yields to the loop once without sleeping.
        await asyncio.sleep(max(0.0, seconds))


# ---------------------------------------------------------------------------
# Schedule (interval | cron) discriminated union
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """Discriminated union: exactly one of ``interval`` or ``cron`` is set.

    Constructed by the :func:`pymodules.scheduled` decorator and stored
    on the decorated function under ``SCHEDULED_ATTR`` for the host to
    discover at registration time.
    """

    interval: timedelta | None = None
    cron: str | None = None

    def __post_init__(self) -> None:
        if (self.interval is None) == (self.cron is None):
            raise TypeError("Schedule requires exactly one of interval= or cron=")
        if self.interval is not None and self.interval.total_seconds() <= 0:
            raise ValueError("Schedule.interval must be a positive timedelta")
        if self.cron is not None:
            # Parse eagerly so a bad cron expression fails at decoration
            # time, not at first fire.
            _CronExpr.parse(self.cron)

    def next_fire_after(self, current: datetime) -> datetime:
        """Compute the next fire time strictly after ``current``."""
        if self.interval is not None:
            return current + self.interval
        assert self.cron is not None  # discriminator invariant
        return _CronExpr.parse(self.cron).next_after(current)


# ---------------------------------------------------------------------------
# Minimal 5-field cron parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CronExpr:
    """Parsed 5-field cron expression: minute hour dom month dow.

    Supported field syntax:

    - ``*`` — all valid values
    - ``N`` — single value
    - ``a-b`` — inclusive range
    - ``a,b,c`` — comma-separated list (each element may be any of the
      forms above)
    - ``*/step`` or ``a-b/step`` — step values

    Not supported (deliberately out of scope for v1):

    - Seconds field (we are minute-resolution)
    - Weekday name aliases (``SUN``, ``MON``, …)
    - ``@hourly`` / ``@daily`` / ``@reboot`` shorthand
    - The ``L`` / ``W`` / ``#`` extensions

    Day-of-week uses 0-6 with Monday=0 (Python ``date.weekday()``); the
    common 0=Sunday convention is deliberately *not* used to keep the
    semantics aligned with Python stdlib.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    _FIELD_RANGES: tuple[tuple[int, int], ...] = (
        (0, 59),  # minute
        (0, 23),  # hour
        (1, 31),  # day of month
        (1, 12),  # month
        (0, 6),  # day of week (Mon=0..Sun=6, Python convention)
    )

    @classmethod
    def parse(cls, expr: str) -> _CronExpr:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron expression must have exactly 5 fields (minute hour "
                f"dom month dow); got {len(parts)}: {expr!r}"
            )
        fields = tuple(
            cls._parse_field(p, lo, hi)
            for p, (lo, hi) in zip(parts, cls._FIELD_RANGES, strict=True)
        )
        return cls(
            minutes=fields[0],
            hours=fields[1],
            days=fields[2],
            months=fields[3],
            weekdays=fields[4],
        )

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> frozenset[int]:
        values: set[int] = set()
        for item in field.split(","):
            # Split off step suffix if present: "*/5" or "0-30/5" or "5/3".
            if "/" in item:
                rng_part, step_part = item.split("/", 1)
                try:
                    step = int(step_part)
                except ValueError as e:
                    raise ValueError(
                        f"invalid step value in cron field {field!r}: {step_part!r}"
                    ) from e
                if step <= 0:
                    raise ValueError(f"step must be positive in cron field {field!r}")
            else:
                rng_part = item
                step = 1

            if rng_part == "*":
                a, b = lo, hi
            elif "-" in rng_part:
                a_str, b_str = rng_part.split("-", 1)
                try:
                    a, b = int(a_str), int(b_str)
                except ValueError as e:
                    raise ValueError(f"invalid range in cron field {field!r}: {rng_part!r}") from e
            else:
                try:
                    a = b = int(rng_part)
                except ValueError as e:
                    raise ValueError(f"invalid value in cron field {field!r}: {rng_part!r}") from e

            if a < lo or b > hi or a > b:
                raise ValueError(f"cron field {field!r} out of range [{lo},{hi}]")

            values.update(range(a, b + 1, step))

        return frozenset(values)

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.day in self.days
            and dt.month in self.months
            and dt.weekday() in self.weekdays
        )

    def next_after(self, current: datetime) -> datetime:
        """Compute the next minute strictly after ``current`` that matches.

        Brute-force minute-by-minute scan with a safety bound. Cron has a
        worst-case period of 4 years (leap-year alignment), so we cap at
        ~5 years of minutes to avoid an infinite loop on a malformed
        expression that somehow slipped past :meth:`parse`.
        """
        # Move to the *next* minute boundary (drop seconds + microseconds).
        candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Safety bound: 5 years of minutes.
        max_iter = 60 * 24 * 366 * 5
        for _ in range(max_iter):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise RuntimeError(f"cron expression has no match within 5 years after {current!r}")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@dataclass
class _Registration:
    """One ``(run_id, method, schedule)`` triple tracked by the scheduler."""

    run_id: str
    method: Callable[..., Any]
    schedule: Schedule
    task: asyncio.Task[Any] | None = None
    # Set true while ``method`` is actively running. Re-entry guard: if a
    # tick fires and ``in_flight`` is true, the tick is skipped and a
    # warning is logged.
    in_flight: bool = False


class Scheduler:
    """In-process scheduler firing ``@scheduled`` Agent methods on time.

    The scheduler is constructed lazily by :class:`ModuleHost` on first
    registration of an Agent template that declares at least one
    ``@scheduled`` method — a host with no scheduled Agents never
    instantiates one (see ``host.scheduler`` accessor).

    Tests inject a mock :class:`Clock` via the constructor to advance
    simulated time without real waits. Production callers leave
    ``clock=None`` and get a :class:`RealClock`.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else RealClock()
        self._registrations: dict[
            tuple[str, str], _Registration
        ] = {}  # (run_id, method_name) -> reg
        self._running: bool = False
        # Predicate the host installs so each scheduled-method loop can
        # self-terminate when its AgentRun is no longer alive. ``None``
        # means "always alive" (useful in unit tests that drive the
        # scheduler directly with no host attached).
        self._is_run_alive: Callable[[str], bool] | None = None

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return self._running

    @property
    def clock(self) -> Clock:
        """The injected :class:`Clock`. Exposed for tests/inspection."""
        return self._clock

    def add(
        self,
        agent_run_id: str,
        method: Callable[..., Any],
        schedule: Schedule,
    ) -> None:
        """Register a scheduled method for an AgentRun.

        If the scheduler is already running, the new method's loop is
        launched immediately. Otherwise it will be launched on the next
        :meth:`start` call.

        ``method`` is the bound method (``getattr(instance, name)``); the
        scheduler is opaque to the underlying Agent — it just invokes
        the callable on its schedule.
        """
        key = (agent_run_id, method.__name__)
        if key in self._registrations:
            # Idempotent re-add: replace with the new schedule (mostly
            # a defensive default; ``host._register_agent`` only calls
            # ``add`` once per (run, method)).
            existing = self._registrations[key]
            if existing.task is not None and not existing.task.done():
                existing.task.cancel()
        reg = _Registration(run_id=agent_run_id, method=method, schedule=schedule)
        self._registrations[key] = reg
        if self._running:
            reg.task = asyncio.get_event_loop().create_task(self._loop(reg))

    def remove(self, agent_run_id: str) -> None:
        """Drop every registration for ``agent_run_id`` and cancel its loops."""
        for key in [k for k in self._registrations if k[0] == agent_run_id]:
            reg = self._registrations.pop(key)
            if reg.task is not None and not reg.task.done():
                reg.task.cancel()

    def start(self) -> None:
        """Launch one loop task per registration.

        Idempotent: calling :meth:`start` while already running is a
        no-op. Returns immediately — the loops run on the current
        event loop.
        """
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        for reg in self._registrations.values():
            if reg.task is None or reg.task.done():
                reg.task = loop.create_task(self._loop(reg))

    def stop(self) -> None:
        """Stop firing and cancel every live loop task.

        Cooperative: each loop reads ``self._running`` after every
        ``clock.sleep`` and returns when it goes false. We *also* cancel
        outstanding tasks so a long ``clock.sleep`` doesn't block
        shutdown. Idempotent.
        """
        if not self._running:
            return
        self._running = False
        for reg in self._registrations.values():
            if reg.task is not None and not reg.task.done():
                reg.task.cancel()

    def set_run_alive_predicate(self, predicate: Callable[[str], bool] | None) -> None:
        """Install a predicate the scheduler consults to decide if a run is alive.

        Called by :class:`ModuleHost` so each scheduled-method loop
        self-exits when its AgentRun has disappeared from
        ``host.agent_runs`` (natural termination, ``run.stop()``, or
        host shutdown). Tests that drive the scheduler standalone leave
        this unset.
        """
        self._is_run_alive = predicate

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self, reg: _Registration) -> None:
        """Per-registration loop: sleep until the *target*, fire, advance, repeat.

        We track ``next_target`` per registration — the scheduled fire
        time, not "now + interval". This gives two properties:

        - On a clock jump (mock-clock ``advance`` covering many
          intervals), each missed target fires in turn with ``delay==0``,
          letting the test observe the catch-up rather than collapsing
          all missed ticks into one. The "no drift correction" pin from
          ADR-0008 / ticket #13 means we advance ``target`` by the
          interval each tick, not by the *delta to now* — so a slow
          fire-body does not skew subsequent fire times.
        - For cron, ``schedule.next_fire_after(target)`` advances to the
          next strictly-later matching minute, which is the spec.

        **Re-entry guard.** Method invocations are launched as a
        separate task (or wrapped sync call) so the loop can keep
        ticking. When a tick comes due while the previous task is
        still alive, the tick is *skipped* (warning logged) and
        ``next_target`` still advances — i.e., the loop never stacks
        invocations and never makes up the missed tick. This matches
        the pin in :mod:`pymodules.scheduler`'s module docstring.
        """
        try:
            # Initialise the first target from the clock's current
            # ``now``. This is the only time we consult ``clock.now``
            # for target computation — subsequent targets derive from
            # the previous target so drift correction is off (the
            # ADR-0008 pin).
            next_target = reg.schedule.next_fire_after(self._clock.now())
            while self._running and self._is_alive(reg.run_id):
                now = self._clock.now()
                delay = (next_target - now).total_seconds()
                # ``clock.sleep`` may complete in zero wall-clock time
                # under a mock clock; the loop is still cooperative
                # because each iteration yields at the ``await`` point.
                await self._clock.sleep(max(0.0, delay))

                # Re-check running / alive after sleep — a stop/remove
                # may have fired during the wait.
                if not self._running or not self._is_alive(reg.run_id):
                    return

                # Re-entry guard: if the previous tick's task is still
                # alive, skip this one. ``in_flight`` is the source of
                # truth (set true before the method runs, cleared in
                # finally) so the check is symmetric with the
                # bookkeeping below.
                if reg.in_flight:
                    scheduler_logger.warning(
                        "Skipping scheduled tick for run_id=%s method=%s: "
                        "previous invocation still in flight",
                        reg.run_id,
                        reg.method.__name__,
                    )
                else:
                    # Launch the invocation as a fire-and-forget task
                    # so the loop can keep ticking while a long-running
                    # method runs. ``in_flight`` flips off in the
                    # wrapper's finally — that's what the next tick's
                    # re-entry guard observes.
                    reg.in_flight = True
                    asyncio.get_event_loop().create_task(self._invoke(reg))
                    # Yield to the loop so the just-launched task gets
                    # a chance to *start*. For a method body that
                    # completes synchronously (or near-synchronously),
                    # this single yield is enough for ``in_flight`` to
                    # flip back to False before the next iteration's
                    # re-entry check. For a method body that genuinely
                    # awaits something slow, control returns here
                    # before that ``await`` resolves and the next tick
                    # observes ``in_flight=True`` → skip. This is the
                    # difference between "fast method, every tick
                    # fires" and "slow method, overlapping ticks
                    # skipped" the spec calls for.
                    await asyncio.sleep(0)

                # Advance the target. ``next_fire_after(next_target)``
                # is correct for both interval and cron: for interval
                # it returns ``next_target + interval``; for cron it
                # walks to the next strictly-later matching minute.
                next_target = reg.schedule.next_fire_after(next_target)
        except asyncio.CancelledError:
            # Normal stop path; swallow so the task finishes cleanly.
            return

    async def _invoke(self, reg: _Registration) -> None:
        """Run one invocation of ``reg.method``, clearing ``in_flight`` on exit.

        Pulled out of :meth:`_loop` so the loop can fire-and-forget
        the call and keep ticking — the re-entry skip behaviour
        relies on the loop *not* blocking on a slow method body.
        """
        try:
            result = reg.method()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — scheduler must not die on user error
            scheduler_logger.exception(
                "Error in scheduled method %s for run_id=%s",
                reg.method.__name__,
                reg.run_id,
            )
        finally:
            reg.in_flight = False

    def _is_alive(self, run_id: str) -> bool:
        if self._is_run_alive is None:
            return True
        try:
            return self._is_run_alive(run_id)
        except Exception:  # noqa: BLE001 — predicate must not crash the loop
            scheduler_logger.exception(
                "is_run_alive predicate raised for run_id=%s; assuming alive",
                run_id,
            )
            return True


__all__ = [
    "Clock",
    "RealClock",
    "Schedule",
    "Scheduler",
]
