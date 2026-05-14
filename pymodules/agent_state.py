"""
AgentStateStore — pluggable per-AgentRun state persistence.

This is the storage seam behind :meth:`AgentRun.checkpoint` (ADR-0008,
PRD #2). The shape deliberately mirrors
:class:`pymodules.resilience.idempotency.IdempotencyStore`:

- A small :class:`typing.Protocol` with ``get`` / ``set`` / ``delete``.
- A bundled :class:`InMemoryAgentStateStore` is the framework default
  installed by the host without user opt-in.
- Persistent backends (Redis, SQL, …) live in future contrib PRDs and
  re-use the conformance test class shipped alongside
  :class:`InMemoryAgentStateStore` in the test tree.

Checkpoint semantics (locked by ADR-0008): the store is written on
explicit :meth:`AgentRun.checkpoint` calls and on AgentRun termination.
**Not** on every attribute write — this matches durable-workflow-engine
semantics (Temporal, Cadence) and keeps per-step cost predictable.

Pin: ``get`` for an unknown ``agent_run_id`` returns ``None`` (not an
empty dict, not a raise). The conformance suite enforces this so future
backends cannot silently drift to one of the other plausible defaults.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentStateStore(Protocol):
    """Per-AgentRun state persistence Protocol.

    Implementations map an ``agent_run_id`` (a UUIDv4 string, per
    ADR-0008) to a ``dict[str, Any]`` snapshot of that run's state. The
    store is *opaque* to the framework — the framework only round-trips
    whatever dict the Agent body chose to assign to ``self._run.state``;
    the serialisation strategy used by a persistent backend is its own
    concern, not the Protocol's.

    Contract:

    - :meth:`get` for an unknown ``agent_run_id`` returns ``None``.
      Implementations must not raise on a miss and must not return an
      empty dict. This is the pinned default — see
      ``tests/test_agent_state_store.py``.
    - :meth:`set` overwrites any existing snapshot for that id.
    - :meth:`delete` is idempotent: deleting an unknown id is a no-op.
    - State for one ``agent_run_id`` is invisible from another id
      (per-instance isolation; ADR-0008 forbids cross-AgentRun state).
    """

    def get(self, agent_run_id: str) -> dict[str, Any] | None:
        """Return the stored state for ``agent_run_id``, or ``None`` on miss.

        Returning ``None`` on miss is the pinned default; implementations
        must not raise and must not synthesise an empty dict.
        """
        ...

    def set(self, agent_run_id: str, state: dict[str, Any]) -> None:
        """Persist ``state`` under ``agent_run_id``, overwriting any prior snapshot."""
        ...

    def delete(self, agent_run_id: str) -> None:
        """Remove the snapshot for ``agent_run_id``. Idempotent on unknown ids."""
        ...


class InMemoryAgentStateStore:
    """Thread-safe in-memory :class:`AgentStateStore`.

    Backed by a ``dict[str, dict[str, Any]]`` guarded by a
    :class:`threading.Lock`, mirroring the locking style of
    :class:`pymodules.resilience.idempotency.InMemoryIdempotencyStore`.
    The lock is held for the duration of dict mutation only; values are
    not copied on the way in or out, so callers who plan to mutate a
    snapshot they pulled from the store should copy it themselves
    (Agent state is normally owned by exactly one AgentRun, so the
    no-copy default avoids gratuitous allocation).

    Suitable as the default — no TTL, no eviction. AgentRun termination
    is responsible for calling :meth:`delete` if the slot should not
    outlive the run; the host writes a terminal snapshot and leaves it
    in place by default so observability tooling can read it after the
    AgentRun has come and gone.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, agent_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._entries.get(agent_run_id)

    def set(self, agent_run_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._entries[agent_run_id] = state

    def delete(self, agent_run_id: str) -> None:
        with self._lock:
            self._entries.pop(agent_run_id, None)

    def clear(self) -> None:
        """Drop all entries. Mostly useful in tests."""
        with self._lock:
            self._entries.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "AgentStateStore",
    "InMemoryAgentStateStore",
]
