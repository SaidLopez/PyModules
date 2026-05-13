# Rename in-process `Event` to `Command`

The in-process primitive that flows through `ModuleHost` is dispatched to **exactly one** winning handler and produces a response. That is command-dispatch semantics, not pub/sub — but the framework's vocabulary (`Event`, `EventInput`, `EventOutput`, "event dispatcher", "events_handled" metric) consistently described it as an event. NetModules has the same naming, and the confusion is well-documented in its issues and forks.

We renamed the in-process primitive to `Command` / `CommandRequest` / `CommandResponse`, with the verb `dispatch`. The word "Event" is reserved for the genuinely event-shaped concept — a fire-and-forget broadcast published to an external broker, addressed to zero-to-many subscribers, with no return value.

## Consequences

- Every existing handler, test, and import path that names `Event`/`EventInput`/`EventOutput` changes. This is a deliberate 1.0 break; there is no compatibility shim because the rename is exactly the point.
- The host method `handle()` becomes `dispatch()`. `Module.handle(command)` retains its name — it's a per-module callback, distinct from the host method.
- The lineage to NetModules is documented but not preserved at the symbol level. Newcomers from NetModules will need a one-paragraph orientation; newcomers without NetModules background get straightforward Python.
