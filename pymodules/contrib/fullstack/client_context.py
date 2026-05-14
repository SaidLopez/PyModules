"""Per-connection client identity for full-stack browser clients.

``ClientContext`` carries the identity of an authenticated browser client —
the entity on the *other end* of an SSE connection or an HTTP dispatch routed
through the cookie auth shim. It is the value the **Outbound policy registry**
(slice #4) consumes when deciding whether a published Event should reach a
given connected client.

Distinction from ``CommandContext``
-----------------------------------

This is deliberately **not** a reuse of ``pymodules.interfaces.CommandContext``
(ADR-0006). Their concerns are different and conflating them would re-create
the meta-bag god-object that ADR-0006 fixed:

- ``CommandContext`` — per-Command observability (``trace_id``,
  ``correlation_id``, ``parent_span_id``). One value per dispatched Command.
- ``ClientContext`` — per-connection identity (``user_id``, ``tenant_id``,
  decoded JWT claims). One value per authenticated browser connection.

The two travel orthogonally: a single browser ``ClientContext`` will originate
many Commands, each with its own ``CommandContext``. Keeping them split keeps
each type single-purpose and greppable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClientContext:
    """Identity of an authenticated browser client.

    Attributes:
        user_id: Stable identifier for the authenticated principal (the JWT
            ``sub`` claim).
        tenant_id: Optional tenant identifier for multi-tenant deployments
            (the JWT ``tenant_id`` claim, if present).
        claims: The full decoded JWT claims dict, available to Outbound
            policies for role-, scope-, or attribute-based filtering.

    The dataclass is frozen — a ``ClientContext`` is constructed once per
    connection from the validated JWT and then passed by reference. Policies
    treat it as immutable.
    """

    user_id: str
    tenant_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


__all__ = ["ClientContext"]
