"""
``examples/agent/`` — one ``Agent`` template, all three trigger modes.

Mirrors the worked example pinned in PRD #2 / ticket #16: an
``OrderProcessor`` Agent template that demonstrates the full ADR-0008
trigger surface on a *single* registered class:

1. **Explicit spawn** — ``host.spawn(OrderProcessor, ...)`` at startup,
   with ``run()`` driving a long-living cooperative loop that honours
   ``self._run._stop_requested`` on every tick.
2. **Event subscription** — ``@subscribes(OrderPlaced, route_by=...)``
   routes incoming Events to a per-``customer_id`` AgentRun. The first
   ``OrderPlaced`` for a new customer spawns a fresh AgentRun; subsequent
   ones with the same ``customer_id`` land on the existing run, so state
   grows across deliveries.
3. **Scheduled** — ``@scheduled(interval=timedelta(seconds=10))`` fires
   a reconciliation tick on every live AgentRun, sharing the same
   ``self.state`` the ``@subscribes`` callback writes to.

The three callbacks all see the *same* ``self`` for a given AgentRun, so
state evolution (orders appended via subscription) is visible to the
reconciliation tick (count printed every 10s) and to ``run()``.

Run with::

    python -m examples.agent.app

Then at the ``> `` prompt:

- ``order <customer_id> <order_id> <total>``  publish an OrderPlaced
- ``runs``                                    list in-flight AgentRuns
- ``quit`` or Ctrl-C                          graceful shutdown
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import timedelta

from pymodules import (
    Agent,
    Event,
    ModuleHost,
    ModuleHostConfig,
    scheduled,
    subscribes,
)

# ---------------------------------------------------------------------------
# Shared Event type
# ---------------------------------------------------------------------------


@dataclass
class OrderPlaced(Event):
    """In-process broadcast: a new order arrived for ``customer_id``.

    The ``customer_id`` field is what ``OrderProcessor``'s
    ``@subscribes(..., route_by=...)`` lambda reads to pick the AgentRun
    this Event lands on — same customer, same AgentRun; new customer,
    new AgentRun.
    """

    order_id: str = ""
    customer_id: str = ""
    total: float = 0.0
    name: str = "orders.placed"


# ---------------------------------------------------------------------------
# The Agent template — one class, three trigger modes
# ---------------------------------------------------------------------------


class OrderProcessor(Agent):
    """Per-customer order-processing saga.

    One AgentRun per ``customer_id``. State lives on ``self.state`` (the
    per-run dict backed by the host's default ``InMemoryAgentStateStore``)
    and is mutated by both the ``@subscribes`` callback (on each new
    order) and ``run()`` (the long-living tick loop), and read by the
    ``@scheduled`` reconciliation method.

    Choice of ``run()`` shape: a long-living cooperative loop. The loop
    sleeps in 100ms slices and re-checks ``self._run._stop_requested``
    between sleeps, so host shutdown's cooperative-grace path
    (``ModuleHostConfig.shutdown_grace``) terminates the run cleanly
    without hitting the hard-cancel + ``AgentRunStuck`` branch. The
    alternative — ``run()`` returning naturally after first idle — would
    make the demo's "AgentRuns disappear on Ctrl-C" narrative invisible
    because the runs would already be gone before shutdown started.

    Choice of state store: the host default (a single shared
    ``InMemoryAgentStateStore`` installed without opt-in). Setting
    ``state_store_factory`` here would not change observable behaviour
    in this demo; the framework default is the path users hit first.
    """

    def __init__(self) -> None:
        super().__init__()
        # ``self.state`` lives on the ``AgentRun`` once we're spawned; on
        # a freshly-constructed instance (the registered template
        # prototype, or this constructor frame), ``self._run`` is None,
        # so the run dict is unreachable here. The host wires
        # ``_run`` immediately after construction; every callback below
        # gates on ``self._run is not None`` to keep type-checkers happy.

    # ------------------------------------------------------------------
    # Trigger mode 1: explicit spawn — ``run()`` is the long-living loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Cooperative tick loop, terminating on ``self._run._stop_requested``.

        On startup we log "processor started" once, then loop until stop
        is requested. Each iteration sleeps 100ms and re-checks the flag,
        so a Ctrl-C during shutdown is honoured within at most one slice.
        """
        assert self._run is not None  # narrow Optional
        run = self._run
        key = run.routing_key or "<startup>"
        print(f"[{key}] processor started (run_id={run.id[:8]})")

        # If a prior tick (e.g. an Event-triggered spawn that fired
        # ``checkpoint()`` before ``run()`` was scheduled) wrote orders
        # already, log them so the demo's "state evolves across triggers"
        # narrative is visible from the first line ``run()`` prints.
        existing = run.state.get("orders", [])
        if existing:
            print(f"[{key}] processor sees {len(existing)} pre-existing order(s)")

        while not run._stop_requested:
            await asyncio.sleep(0.1)

        print(f"[{key}] processor stopping (run_id={run.id[:8]})")

    # ------------------------------------------------------------------
    # Trigger mode 2: ``@subscribes`` with ``route_by`` — per-customer
    # ------------------------------------------------------------------

    @subscribes(OrderPlaced, route_by=lambda e: e.customer_id)
    def on_order_placed(self, event: OrderPlaced) -> None:
        """Route ``OrderPlaced`` to the per-``customer_id`` AgentRun.

        First Event for a given ``customer_id`` spawns a fresh AgentRun;
        subsequent Events with the same key land on the existing one.
        The framework wires this from the ``route_by=`` lambda — we do
        not have to maintain the per-customer table ourselves.
        """
        assert self._run is not None
        run = self._run
        orders: list[dict[str, object]] = run.state.setdefault("orders", [])
        orders.append(
            {
                "order_id": event.order_id,
                "total": event.total,
            }
        )
        # Make the new state durable. The store is a no-op when none is
        # wired (unit tests), and the InMemoryAgentStateStore the host
        # installs by default makes terminal-state observable to
        # subsequent inspection.
        run.checkpoint()
        print(
            f"[{event.customer_id}] order received "
            f"({event.order_id}, ${event.total:.2f}); "
            f"customer now has {len(orders)} order(s)"
        )

    # ------------------------------------------------------------------
    # Trigger mode 3: ``@scheduled`` — reconciliation tick every 10s
    # ------------------------------------------------------------------

    @scheduled(interval=timedelta(seconds=10))
    def reconcile(self) -> None:
        """Periodic state-summary log for this AgentRun.

        The scheduler fires this on every live AgentRun of the
        ``OrderProcessor`` template — including the explicit-spawn run
        that has no ``routing_key`` set. The shared-state demonstration:
        the count printed here is the *same* list the ``@subscribes``
        callback mutated above, on the same ``self`` instance.
        """
        assert self._run is not None
        run = self._run
        order_count = len(run.state.get("orders", []))
        print(
            f"reconcile: customer={run.routing_key}, "
            f"order_count={order_count}"
        )


# ---------------------------------------------------------------------------
# CLI driver — keeps the event loop alive so the scheduler can fire
# ---------------------------------------------------------------------------


HELP = """\
Commands:
  order <customer_id> <order_id> <total>   publish an OrderPlaced Event
  runs                                     list in-flight AgentRuns
  help                                     show this help
  quit / exit / Ctrl-D                     graceful shutdown
"""


def publish_order(
    host: ModuleHost,
    customer_id: str,
    order_id: str,
    total: float,
) -> None:
    """Publish a single ``OrderPlaced`` Event onto the host's EventBus.

    Pulled out of the REPL loop so this module is also importable as a
    library: a notebook or a future automated smoke test can call
    ``publish_order(host, ...)`` directly without going through the CLI.
    """
    host.publish(
        OrderPlaced(
            order_id=order_id,
            customer_id=customer_id,
            total=total,
        )
    )


async def _repl(host: ModuleHost) -> None:
    """Tiny async REPL driving the demo from the terminal.

    We run ``input()`` on a worker thread via ``asyncio.to_thread`` so
    the event loop keeps spinning — the scheduler's reconciliation
    ticks need a live loop to fire on, and so do any async subscribers.
    """
    print()
    print(HELP)
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
        except EOFError:
            # Ctrl-D — treat as graceful quit.
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in {"quit", "exit"}:
            return
        if line == "help":
            print(HELP)
            continue
        if line == "runs":
            runs = list(host.agent_runs.values())
            if not runs:
                print("(no AgentRuns in flight)")
                continue
            for r in runs:
                key = r.routing_key or "<startup>"
                orders = r.state.get("orders", [])
                print(f"  {r.id[:8]}  {r.template.__name__}  customer={key}  orders={len(orders)}")
            continue
        if line.startswith("order"):
            parts = line.split()
            if len(parts) != 4:
                print("usage: order <customer_id> <order_id> <total>")
                continue
            _, customer_id, order_id, total_str = parts
            try:
                total = float(total_str)
            except ValueError:
                print(f"invalid total: {total_str!r}")
                continue
            publish_order(host, customer_id, order_id, total)
            continue
        print(f"unknown command: {line!r} — type 'help'")


async def main() -> None:
    """Build the host, register the template, spawn the startup run, REPL."""
    config = ModuleHostConfig(
        # Short grace so Ctrl-C feels snappy in the demo. The cooperative
        # loop in ``run()`` checks the stop flag every 100ms, so 2s is
        # plenty.
        shutdown_grace=2.0,
    )
    host = ModuleHost(config=config)
    host.register(OrderProcessor())

    # Explicit spawn on startup — this is trigger mode 1. We pass an
    # explicit ``routing_key`` so this AgentRun is distinguishable in
    # ``runs`` output and in reconciliation logs, even though no
    # ``OrderPlaced`` Event ever lands on it.
    host.spawn(OrderProcessor, routing_key="setup-demo")

    try:
        await _repl(host)
    finally:
        # Run shutdown on a worker thread because the sync ``shutdown()``
        # path walks the per-run completion events from a *non*-loop
        # thread (see ModuleHost._await_agent_shutdown). Calling it
        # directly here would trip the "shutdown called from inside a
        # running event loop" warning and skip the grace-period wait.
        print("\nshutting down…")
        await asyncio.to_thread(host.shutdown)
        print("shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # ``asyncio.run`` cancels ``main`` cleanly on Ctrl-C; we land
        # here after the ``finally`` block above has already torn the
        # host down. Print nothing — the shutdown messages above are
        # enough.
        sys.exit(0)
