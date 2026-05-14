# `examples/agent/` — one Agent template, all three trigger modes

The runnable counterpart to the integration tests for PyModules' **Agent**
primitive (ADR-0008 / PRD #2). A single `OrderProcessor` class
demonstrates every trigger mode on the same `self`:

| Trigger                                 | Where it shows up                                                              |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| **Explicit spawn**                      | `host.spawn(OrderProcessor, routing_key="setup-demo")` at startup; `run()` is the long-living loop. |
| **Event subscription** (`@subscribes`)  | `OrderPlaced` Events route by `customer_id` — one AgentRun per customer.       |
| **Scheduled** (`@scheduled`)            | `reconcile()` fires every 10s on every live AgentRun and logs its order count. |

All three callbacks share the same `self.state` per AgentRun. The
`InMemoryAgentStateStore` the host installs by default is the persistence
backend; `run.checkpoint()` is called inside the `@subscribes` handler
on every order so the snapshot is durable from the moment it lands.

## Install

From the repo root:

```bash
pip install -e .
```

(No `[fullstack]` / `[api]` extras needed — this demo is pure
`pymodules` + stdlib.)

## Run

```bash
python -m examples.agent.app
```

You will see the startup log line and then a `>` prompt:

```
[setup-demo] processor started (run_id=...)

Commands:
  order <customer_id> <order_id> <total>   publish an OrderPlaced Event
  runs                                     list in-flight AgentRuns
  help                                     show this help
  quit / exit / Ctrl-D                     graceful shutdown
>
```

Wait ~10 seconds and the scheduled reconciliation tick fires for the
startup AgentRun:

```
reconcile: customer=setup-demo, order_count=0
```

## Drive the demo

Publish an order for a new customer:

```
> order cust-alice ord-1 49.99
[cust-alice] order received (ord-1, $49.99); customer now has 1 order(s)
```

A new AgentRun was spawned for `cust-alice` (the `route_by=lambda e:
e.customer_id` lambda hashed to a fresh key). Inspect:

```
> runs
  abcd1234  OrderProcessor  customer=setup-demo  orders=0
  ef567890  OrderProcessor  customer=cust-alice  orders=1
```

Send another order for the **same** customer — the existing AgentRun
receives it and state grows:

```
> order cust-alice ord-2 12.50
[cust-alice] order received (ord-2, $12.50); customer now has 2 order(s)
```

Send an order for a **different** customer — a fresh AgentRun is
spawned:

```
> order cust-bob ord-3 7.25
[cust-bob] order received (ord-3, $7.25); customer now has 1 order(s)
> runs
  abcd1234  OrderProcessor  customer=setup-demo  orders=0
  ef567890  OrderProcessor  customer=cust-alice  orders=2
  90abcdef  OrderProcessor  customer=cust-bob    orders=1
```

Watch the next reconciliation tick — it fires once per live AgentRun,
sharing each one's state:

```
reconcile: customer=setup-demo, order_count=0
reconcile: customer=cust-alice, order_count=2
reconcile: customer=cust-bob, order_count=1
```

## Graceful shutdown

Type `quit`, press **Ctrl-D**, or hit **Ctrl-C**. Each AgentRun's
cooperative tick loop in `run()` polls `self._run._stop_requested`
every 100ms; the host's `shutdown_grace` (set to 2s in this demo) is
plenty of time for them to honour it, so you see the clean
"processor stopping" log line for every live AgentRun and no
`AgentRunStuck` / `AgentFailed` event is published:

```
> quit

shutting down…
[setup-demo] processor stopping (run_id=...)
[cust-alice] processor stopping (run_id=...)
[cust-bob] processor stopping (run_id=...)
shutdown complete
```

## What to look at next

- The `OrderProcessor` template in `app.py` is annotated with the design
  choices: long-running `run()` shape, `route_by=` per-customer keying,
  default state store, cooperative-stop sleep granularity.
- The end-to-end behaviour this demo exercises also has unit and
  integration test coverage under `tests/test_agent.py` and
  `tests/integration/test_agent_integration.py` — useful when adapting
  the pattern to your own Agent.
