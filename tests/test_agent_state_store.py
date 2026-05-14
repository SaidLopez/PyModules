"""
Tests for :class:`pymodules.AgentStateStore` and
:class:`pymodules.InMemoryAgentStateStore`, plus the
:meth:`AgentRun.checkpoint` semantics (ticket #12 / ADR-0008).

The :class:`StoreConformance` mixin is the load-bearing artefact in this
file: every backend implementation (the bundled in-memory one and any
future contrib backend) inherits from it and supplies a one-line
:meth:`make_store` so the full Protocol contract is re-tested without
duplication.

Pin (re-pinned here so it cannot drift): ``get`` for an unknown
``agent_run_id`` returns ``None`` — not ``{}``, not a raise.
``InMemoryAgentStateStore`` is the reference implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pymodules import (
    Agent,
    AgentStateStore,
    InMemoryAgentStateStore,
    ModuleHost,
)

# ---------------------------------------------------------------------------
# Conformance suite — subclass with one line per backend
# ---------------------------------------------------------------------------


class StoreConformance:
    """Reusable Protocol-conformance tests for :class:`AgentStateStore`.

    Future persistent backends (Redis, SQL) drop a subclass:

        class TestRedisStore(StoreConformance):
            def make_store(self) -> AgentStateStore:
                return RedisAgentStateStore(url=os.environ["REDIS_URL"])

    and inherit the entire contract test set unchanged. Tests in this
    class must only touch the Protocol surface — no in-memory-specific
    attributes (``_entries``, ``size``, …).
    """

    def make_store(self) -> AgentStateStore:
        raise NotImplementedError("Subclasses of StoreConformance must override make_store().")

    def test_protocol_compliance(self) -> None:
        """``isinstance(store, AgentStateStore)`` is True (runtime_checkable)."""
        store = self.make_store()
        assert isinstance(store, AgentStateStore)

    def test_set_then_get_round_trip(self) -> None:
        """``set`` then ``get`` returns the same dict for the same id."""
        store = self.make_store()
        payload: dict[str, Any] = {"step": 3, "scratch": ["a", "b"]}
        store.set("run-1", payload)

        got = store.get("run-1")
        assert got == payload

    def test_get_unknown_id_returns_none(self) -> None:
        """The pinned default: a miss returns ``None``, not an empty dict, not a raise."""
        store = self.make_store()
        # Sanity: a fresh store with no writes is a miss for any id.
        assert store.get("never-written") is None

        # Even after unrelated writes, an unknown id is still a None-miss.
        store.set("known", {"x": 1})
        assert store.get("unknown") is None

    def test_set_overwrites_existing_state(self) -> None:
        """A second ``set`` for the same id replaces the previous snapshot."""
        store = self.make_store()
        store.set("run-1", {"v": 1})
        store.set("run-1", {"v": 2, "extra": "y"})

        assert store.get("run-1") == {"v": 2, "extra": "y"}

    def test_delete_then_get_returns_none(self) -> None:
        """After ``delete`` a known id reads back as a miss (``None``)."""
        store = self.make_store()
        store.set("run-1", {"v": 1})
        assert store.get("run-1") == {"v": 1}

        store.delete("run-1")
        assert store.get("run-1") is None

    def test_delete_unknown_id_is_idempotent(self) -> None:
        """``delete`` on an unknown id must not raise."""
        store = self.make_store()
        # Must not raise.
        store.delete("never-existed")
        store.delete("never-existed")  # twice for good measure.

    def test_isolation_between_ids(self) -> None:
        """State set under one id is invisible under another id.

        ADR-0008 forbids cross-AgentRun state; the conformance suite
        proves backends honour that at the Protocol level.
        """
        store = self.make_store()
        store.set("a", {"who": "alice"})
        store.set("b", {"who": "bob"})

        assert store.get("a") == {"who": "alice"}
        assert store.get("b") == {"who": "bob"}

        store.delete("a")
        # Deleting one must not affect the other.
        assert store.get("a") is None
        assert store.get("b") == {"who": "bob"}

    def test_empty_dict_is_a_legitimate_value(self) -> None:
        """An empty-dict snapshot is *not* the same as a miss.

        ``store.set(id, {})`` then ``store.get(id)`` returns ``{}``, not
        ``None`` — the miss/None pin is reserved for "id was never
        written or has been deleted".
        """
        store = self.make_store()
        store.set("run-1", {})
        assert store.get("run-1") == {}


class TestInMemoryAgentStateStore(StoreConformance):
    """Conformance suite, bound to the bundled in-memory implementation."""

    def make_store(self) -> AgentStateStore:
        return InMemoryAgentStateStore()


class TestInMemoryAgentStateStoreExtras:
    """In-memory-specific helpers (``clear``, ``size``) not in the Protocol."""

    def test_clear_drops_all_entries(self) -> None:
        store = InMemoryAgentStateStore()
        store.set("a", {"v": 1})
        store.set("b", {"v": 2})
        assert store.size == 2

        store.clear()
        assert store.size == 0
        assert store.get("a") is None
        assert store.get("b") is None

    def test_size_reflects_writes_and_deletes(self) -> None:
        store = InMemoryAgentStateStore()
        assert store.size == 0
        store.set("a", {})
        assert store.size == 1
        store.set("a", {"v": 1})  # overwrite, same id — still size 1
        assert store.size == 1
        store.set("b", {})
        assert store.size == 2
        store.delete("a")
        assert store.size == 1


# ---------------------------------------------------------------------------
# Checkpoint semantics — state is NOT persisted on every attribute write
# ---------------------------------------------------------------------------


class _CountingStore:
    """In-memory store wrapper that counts ``set`` / ``get`` / ``delete`` calls.

    Used by the checkpoint-semantics tests to assert ``set`` happens
    exactly when the contract demands (on ``checkpoint()`` and on
    termination) and **not** on every attribute write inside ``run()``.
    """

    def __init__(self) -> None:
        self._inner = InMemoryAgentStateStore()
        self.set_calls: int = 0
        self.get_calls: int = 0
        self.delete_calls: int = 0
        self.set_history: list[tuple[str, dict[str, Any]]] = []

    def get(self, agent_run_id: str) -> dict[str, Any] | None:
        self.get_calls += 1
        return self._inner.get(agent_run_id)

    def set(self, agent_run_id: str, state: dict[str, Any]) -> None:
        self.set_calls += 1
        # Copy on the way in so we record the snapshot at this moment,
        # not whatever the Agent body later mutates it to.
        self.set_history.append((agent_run_id, dict(state)))
        self._inner.set(agent_run_id, state)

    def delete(self, agent_run_id: str) -> None:
        self.delete_calls += 1
        self._inner.delete(agent_run_id)


async def _drain(host: ModuleHost, run_id: str, max_yields: int = 100) -> None:
    """Yield the event loop until ``run_id`` leaves ``host.agent_runs``."""
    for _ in range(max_yields):
        if run_id not in host.agent_runs:
            return
        await asyncio.sleep(0)


class TestCheckpointSemantics:
    """``checkpoint()`` and termination are the only durable-write triggers."""

    async def test_attribute_writes_alone_do_not_persist(self) -> None:
        """Mutating ``self._run.state`` does NOT call ``store.set``.

        This is the load-bearing contract from ADR-0008: state writes
        are cheap (in-memory dict ops); persistence is opt-in per write
        via ``checkpoint()``.
        """
        counting = _CountingStore()

        class MutatingAgent(Agent):
            state_store_factory = staticmethod(lambda: counting)

            async def run(self) -> None:
                assert self._run is not None
                # Many mutations, zero checkpoints. The implicit
                # termination write is the only ``set`` that should
                # land in the store.
                self._run.state["step"] = 1
                self._run.state["step"] = 2
                self._run.state["step"] = 3
                self._run.state["scratch"] = ["a", "b", "c"]

        host = ModuleHost()
        host.register(MutatingAgent())
        run = host.spawn(MutatingAgent)

        await _drain(host, run.id)

        # Exactly one ``set`` — the terminal write on cleanup. The three
        # in-body mutations did not touch the store.
        assert counting.set_calls == 1
        # And that single set reflects the *last* state, not any
        # intermediate value.
        assert counting.set_history[-1] == (
            run.id,
            {"step": 3, "scratch": ["a", "b", "c"]},
        )

    async def test_explicit_checkpoint_persists_current_state(self) -> None:
        """``self._run.checkpoint()`` writes the current ``state`` to the store."""
        counting = _CountingStore()

        class CheckpointingAgent(Agent):
            state_store_factory = staticmethod(lambda: counting)

            async def run(self) -> None:
                assert self._run is not None
                self._run.state["phase"] = "alpha"
                self._run.checkpoint()
                self._run.state["phase"] = "beta"
                self._run.checkpoint()
                # Final mutation without checkpoint — picked up by the
                # implicit terminal write.
                self._run.state["phase"] = "gamma"

        host = ModuleHost()
        host.register(CheckpointingAgent())
        run = host.spawn(CheckpointingAgent)

        await _drain(host, run.id)

        # Two explicit checkpoints + one terminal write = 3 sets.
        assert counting.set_calls == 3
        phases = [snapshot["phase"] for _, snapshot in counting.set_history]
        assert phases == ["alpha", "beta", "gamma"]

    async def test_terminal_write_persists_even_when_run_raises(self) -> None:
        """An unhandled exception in ``run()`` still triggers the terminal write.

        The host's cleanup is in a ``finally``: state at the moment of
        the raise is observable via the store afterwards.
        """
        counting = _CountingStore()

        class FailingAgent(Agent):
            state_store_factory = staticmethod(lambda: counting)

            async def run(self) -> None:
                assert self._run is not None
                self._run.state["progress"] = "partial"
                raise RuntimeError("boom")

        host = ModuleHost()
        host.register(FailingAgent())
        run = host.spawn(FailingAgent)

        await _drain(host, run.id)

        # One terminal write, capturing the partial state.
        assert counting.set_calls == 1
        assert counting.set_history[-1] == (run.id, {"progress": "partial"})

    async def test_checkpoint_no_store_is_safe(self) -> None:
        """``checkpoint()`` is a no-op when the AgentRun has no store wired.

        Constructing an ``AgentRun`` standalone (the unit-test seam) with
        ``state_store=None`` must allow ``checkpoint()`` calls without
        raising — otherwise unit-test ergonomics suffer.
        """
        from pymodules.agent import AgentRun

        run = AgentRun(Agent(), host=object(), state_store=None)  # type: ignore[arg-type]
        run.state["x"] = 1
        # Must not raise.
        run.checkpoint()

    async def test_state_is_initialised_empty(self) -> None:
        """A fresh ``AgentRun`` has ``state == {}``."""
        from pymodules.agent import AgentRun

        run = AgentRun(Agent(), host=object())  # type: ignore[arg-type]
        assert run.state == {}


# ---------------------------------------------------------------------------
# Per-template state_store_factory override + host default
# ---------------------------------------------------------------------------


class TestStateStoreSelection:
    """Per-template ``state_store_factory`` overrides the host default."""

    async def test_default_store_used_when_factory_is_none(self) -> None:
        """An Agent with no ``state_store_factory`` writes to the host default."""

        class DefaultAgent(Agent):
            async def run(self) -> None:
                assert self._run is not None
                self._run.state["k"] = "v"
                self._run.checkpoint()

        host = ModuleHost()
        host.register(DefaultAgent())
        run = host.spawn(DefaultAgent)

        await _drain(host, run.id)

        # The host's default store carries the snapshot.
        assert host._default_state_store.get(run.id) == {"k": "v"}

    async def test_factory_overrides_default_for_that_template(self) -> None:
        """When ``state_store_factory`` is set, the factory's store is used.

        The host default sees no writes for that template's spawns.
        """
        per_template = InMemoryAgentStateStore()

        class OverridingAgent(Agent):
            state_store_factory = staticmethod(lambda: per_template)

            async def run(self) -> None:
                assert self._run is not None
                self._run.state["who"] = "overrider"
                self._run.checkpoint()

        host = ModuleHost()
        host.register(OverridingAgent())
        run = host.spawn(OverridingAgent)

        await _drain(host, run.id)

        # The factory's store has the snapshot.
        assert per_template.get(run.id) == {"who": "overrider"}
        # The host default does not — it never saw this AgentRun.
        assert host._default_state_store.get(run.id) is None

    async def test_factory_called_per_spawn(self) -> None:
        """The factory is invoked once per spawn, not once per template."""

        call_count = {"n": 0}
        # Each call returns a fresh store, so spawn N gets its own.
        stores: list[InMemoryAgentStateStore] = []

        def factory() -> AgentStateStore:
            call_count["n"] += 1
            s = InMemoryAgentStateStore()
            stores.append(s)
            return s

        class PerSpawnAgent(Agent):
            state_store_factory = staticmethod(factory)

            async def run(self) -> None:
                assert self._run is not None
                self._run.state["spawn"] = call_count["n"]
                self._run.checkpoint()

        host = ModuleHost()
        host.register(PerSpawnAgent())

        run_a = host.spawn(PerSpawnAgent)
        run_b = host.spawn(PerSpawnAgent)
        await _drain(host, run_a.id)
        await _drain(host, run_b.id)

        assert call_count["n"] == 2
        # Each spawn wrote into its own store — isolation by construction.
        assert stores[0].size + stores[1].size == 2
        # No store carries both ids.
        for s in stores:
            ids_with_state = [k for k in (run_a.id, run_b.id) if s.get(k) is not None]
            assert len(ids_with_state) == 1


# Async-test plumbing: pytest-asyncio is configured in auto mode
# (``asyncio_mode = "auto"`` in pyproject.toml), so bare ``async def``
# test methods are picked up automatically. No per-test marker required.
