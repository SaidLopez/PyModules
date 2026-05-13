# In-process `EventBus` as a first-class core primitive

ADR-0001 reserved "Event" for cross-process, broker-mediated fan-out: an Event was something a Module published to Redis/Kafka via a contrib package, distinct from the in-process Command. That boundary made sense when the only fan-out concern was crossing a process boundary. In practice it had a hole — in-process listeners had nowhere to live.

The Maintainer's worked example in `CONTEXT.md` ("publish a `UserCreated` Event after `CreateUser` succeeds; the audit log subscribes to it") was the canonical onboarding scenario, and it was awkward to satisfy. The audit-log subscriber could not become a second Module that `@handles(CreateUser)` — only one Module wins a Command (ADR-0003). It could subscribe over a real broker, but that forced Redis (or similar) into the dependency tree of a single-process application. It could share state with the `CreateUser` handler via globals, which is what users were actually doing — and that is not a framework, it is a workaround.

We promote the in-process pub/sub primitive to core. `Event` is now a first-class dataclass (sibling of `Command` / `CommandRequest` / `CommandResponse`) and `EventBus` is the in-process fan-out registry. The broker-mediated cross-process case from ADR-0001 still exists and still belongs in contrib; the `Event` type is now the same Python class on both sides of that boundary, so a Module that publishes locally today can later be swapped to publish across processes without changing its payload definitions.

`Event` and `Command` remain deliberately distinct primitives. They are not collapsed into "one dispatch mechanism with different semantics flags":

  - `Command` runs through a configurable middleware chain (rate limit, circuit breaker, retry, idempotency, metrics, tracing). Each cross-cutting concern is composable. One module wins; the response value flows back to the caller.
  - `Event` does not run through that chain. Fire-and-forget broadcast has no return channel, retry against a side-effecting subscriber is unsafe without idempotency the framework cannot generically supply, and rate-limiting a *subscriber* (not the publisher) is a different shape from rate-limiting a Command. The EventBus stays small and predictable; observability/retry concerns that a particular subscriber needs can be implemented inside that subscriber.

## Design points

**Exact-type routing, O(1) lookup.** Subscribers are indexed by `type(event)`, the same principle as the Command dispatch table (ADR-0003). A subscriber to `BaseEvent` does **not** receive a `DerivedEvent`; if the user wants both, they subscribe to both classes explicitly. Inheritance fan-out was rejected for the same reasons the Command registry rejected predicates: it makes "who handles this" non-obvious from one line of registration, it walks an MRO on every publish, and it invites the "should grandparent receive it too?" question that has no objective answer. Users who need polymorphism create a deliberate base class and route to it explicitly, the same way they would split a Command into narrower classes.

**Error isolation, not propagation.** A raise in one subscriber is caught, logged via `eventbus_logger`, and swallowed; subsequent subscribers still receive the event. The publisher has no return channel for subscriber errors — re-raising on the publisher's call site would force every caller of `host.publish(...)` to write `try/except` to defend against arbitrary unrelated subscribers, defeating fire-and-forget. Subscribers that need durable error handling (a DLQ, an alert) write that handling themselves. A future enhancement could supply an `on_subscriber_error` hook for observability without changing the fire-and-forget contract.

**No middleware chain on the EventBus in v1.** A `RetryMiddleware`-equivalent on the EventBus is not safe by default: retrying a side-effecting subscriber without idempotency duplicates effects, and the framework cannot synthesise idempotency keys for arbitrary user code. A `MetricsMiddleware` equivalent is reasonable but is not worth a generic middleware contract when a `LoggingSubscriber` wrapper achieves the same outcome with no machinery. If concrete demand appears for cross-cutting concerns on subscribers (per-subscriber timeout, per-subscriber metrics) we add a `wrap_subscriber(callable) -> callable` factory rather than re-inventing the dispatch chain.

**ModuleHost owns one EventBus (option a).** The alternatives considered were (a) `ModuleHost` constructs an `EventBus` internally and auto-wires `@subscribes` methods of registered Modules, vs (b) `EventBus` is constructed independently and passed in via `ModuleHostConfig`. We picked (a):

  - Same lifetime, same registration step as `@handles`. A user adding a `@subscribes` method to an existing Module does not edit host construction code.
  - `host.publish(event)` reads naturally as the symmetric verb to `host.dispatch(command)`.
  - `CONTEXT.md` already says "`ModuleHost` holds no broker or persistence concerns". An in-process `EventBus` is neither a broker (no transport, no serialisation) nor persistence (no durability across process restart); it is purely a fan-out registry. Owning it does not violate that rule.

A user who wants two independent registries (e.g., partitioning audit events from domain events) can still construct standalone `EventBus` instances and hold them on their Modules — option (a) does not preclude that.

**No auto-publish.** The framework never publishes an event on the caller's behalf after a Command succeeds. Modules publish explicitly inside their handler — `host.publish(UserCreated(...))`. The same pattern users would write today with an external broker, minus the broker. Inferred-publish was tempting (decorate the Command with the events it implies) but it makes the call graph invisible: a reader can no longer answer "what happens when this Command runs" from the handler body alone.

**Sync and async subscribers on both publish paths.** `publish(event)` is the sync facade; sync subscribers run inline, async subscribers are bridged via `asyncio.run`. `publish_async(event)` runs in the caller's loop; async subscribers are awaited sequentially, sync subscribers run inline on the calling task. Sequential delivery (rather than `asyncio.gather`) preserves the property that registration order = invocation order and that one slow subscriber does not race another to a shared resource. Parallel delivery can be opted into by a subscriber that wraps its own work in `asyncio.create_task`.

## Consequences

- A new public surface: `Event`, `EventBus`, `@subscribes`, `host.publish(...)`, `host.publish_async(...)`, `host.event_bus`. The `subscribes` decorator is a sibling of `handles`. A Module may carry both kinds of methods.
- The `Event` glossary entry in `CONTEXT.md` is split into the in-process case (this ADR) and the cross-process case (broker, ADR-0001). The vocabulary "publish" / "subscribe" / "subscriber" applies to both; the transport (in-process EventBus vs broker) is the orthogonal axis.
- Contrib brokers (`pymodules.contrib.messaging`) continue to exist for the cross-process case and remain Module-owned. A contrib package may, in a future release, supply a `BridgeSubscriber` that subscribes to a local `Event` class on the host's EventBus and forwards it to a broker — making "publish locally" and "publish remotely" the same call site. That bridge is out of scope here.
- Tests that previously asserted "only one Module handles `X`" must remain — Commands still have one winner. The new EventBus tests cover the multi-subscriber case that has no analogue in Command dispatch.
