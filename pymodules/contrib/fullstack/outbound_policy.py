"""Outbound policy registry + ``@outbound_policy`` decorator.

The Outbound policy mechanism is the cross-tenant safety story for the
fullstack contrib's SSE push channel: **no Event reaches a connected
browser client unless the publishing Module has explicitly declared a
policy callable for that Event class that returns ``True`` for that
client's context**.

Architectural placement:

- The registry lives **outside** the in-process EventBus, preserving
  ADR-0007's "no middleware on the bus" contract. ``EventBus.publish``
  fan-out is unconditional; the SSE layer (later slice) consults this
  registry per-Event-per-client *above* the bus.
- The registry is owned by :class:`pymodules.host.ModuleHost`, exposed
  via the lazy ``host.outbound_policies`` property — hosts that never
  declare a published Event or an ``@outbound_policy``-decorated method
  never instantiate one.
- Per-Module wiring is declarative: a Module decorates a method with
  ``@outbound_policy(SomeEvent)`` and :class:`ModuleHost.register`
  scans the class at registration time, mirroring the existing
  ``@handles`` / ``@subscribes`` machinery.

Deny-by-default semantics:

- :meth:`OutboundPolicyRegistry.apply` returns ``False`` for any Event
  class with no registered policy. This matches the PRD's
  "deny-by-default outbound" framing — forgetting to register a policy
  means the Event never reaches a client, which is the safe failure
  mode. The SSE endpoint (later slice) still rejects subscriptions to
  unpolicied Events up-front with a loud HTTP 400 so the bug is found
  at subscribe time, not silently masked at runtime; this method is
  the second line of defence behind that check.
- Conversely, double-registration without ``override=True`` raises
  :class:`OutboundPolicyConflict`. Silent last-writer-wins on an
  Event's outbound filter would be a cross-tenant leakage footgun, so
  the framework defaults to loud rejection (mirrors the existing
  ``DuplicateCommandError`` guard for ``@handles``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from pymodules.interfaces import Event

from .exceptions import OutboundPolicyConflict

if TYPE_CHECKING:
    # ``ClientContext`` is defined in the cookie-auth slice (#5). It lives
    # in ``client_context.py`` (the cookie-auth slice splits the small
    # frozen dataclass out from the FastAPI-dependent ``cookie_auth.py``
    # so the type can be referenced without pulling FastAPI in). Guarded
    # under TYPE_CHECKING so this module's own runtime imports stay
    # total even if the cookie-auth slice is partially landed.
    from .client_context import ClientContext  # noqa: F401

# Marker attribute name written onto methods decorated by ``@outbound_policy``.
# Read by ``ModuleHost.register`` (and the registry's scan helper) to wire
# the bound method into the host's :class:`OutboundPolicyRegistry`. Mirrors
# the ``HANDLES_ATTR`` / ``SUBSCRIBES_ATTR`` markers in
# :mod:`pymodules.module`.
OUTBOUND_POLICY_ATTR = "__pymodules_outbound_policy__"

# Policy callable shape. Documented here as a type alias rather than an
# inline annotation so the SSE / manifest slices can ``from .outbound_policy
# import OutboundPolicy`` and stay aligned.
#
# A policy receives the Event instance about to be pushed and the
# ``ClientContext`` of a candidate connected client; it returns ``True`` to
# allow delivery to that client, ``False`` to suppress it.
OutboundPolicy = Callable[[Event, Any], bool]

E = TypeVar("E", bound=Event)
F = TypeVar("F", bound=Callable[..., bool])


def outbound_policy(
    event_cls: type[Event],
) -> Callable[[F], F]:
    """Decorator marking a :class:`pymodules.Module` method as an outbound policy.

    The decorated method's signature is::

        def policy(self, event: <event_cls>, client: ClientContext) -> bool: ...

    Returning ``True`` allows the Event to be pushed to that client over
    the SSE channel; ``False`` suppresses it. The policy callable runs
    once per (Event, candidate client) pair — keep it cheap.

    :class:`pymodules.host.ModuleHost.register` scans each registered
    Module's class for methods carrying the
    :data:`OUTBOUND_POLICY_ATTR` marker (the Event class) and wires the
    bound method into ``host.outbound_policies``. Re-decorating the same
    Event class on two different Modules — or twice on the same Module —
    raises :class:`OutboundPolicyConflict` at registration time unless
    ``host.register(..., override=True)`` (future surface) is used.

    Mirrors the style of :func:`pymodules.module.handles` and
    :func:`pymodules.module.subscribes` — a thin marker decorator that
    stores the claimed type on the function, with the actual wiring
    done by the host at registration time.

    Example::

        class ChatModule(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate_message_posted(
                self, event: MessagePosted, client: ClientContext
            ) -> bool:
                return event.tenant_id == client.tenant_id
    """
    if not (isinstance(event_cls, type) and issubclass(event_cls, Event)):
        raise TypeError(
            f"@outbound_policy argument must be an Event subclass; got {event_cls!r}"
        )

    def decorator(func: F) -> F:
        setattr(func, OUTBOUND_POLICY_ATTR, event_cls)
        return func

    return decorator


class OutboundPolicyRegistry:
    """Per-host registry of ``EventCls -> outbound policy callable``.

    Owned by :class:`pymodules.host.ModuleHost` and exposed via the
    lazy ``host.outbound_policies`` property. The SSE layer (later
    slice) consults it per published Event per connected client.

    The registry is intentionally tiny: a dict, a guarded ``register``
    and a deny-by-default ``apply``. All policy semantics live in the
    callables the calling Module supplies.
    """

    def __init__(self) -> None:
        self._policies: dict[type[Event], OutboundPolicy] = {}

    def register(
        self,
        event_cls: type[Event],
        policy: OutboundPolicy,
        *,
        override: bool = False,
    ) -> None:
        """Register ``policy`` as the outbound filter for ``event_cls``.

        Raises:
            OutboundPolicyConflict: ``event_cls`` already has a policy
                and ``override=False``. Mirrors the
                :class:`pymodules.exceptions.DuplicateCommandError`
                guard on ``@handles`` — silent last-writer-wins on an
                outbound filter would be a cross-tenant leakage
                footgun, so we reject loudly by default.
            TypeError: ``event_cls`` is not a subclass of
                :class:`pymodules.Event`.

        Pass ``override=True`` to deliberately replace an existing
        policy (e.g. for test doubles or hot-reloads).
        """
        if not (isinstance(event_cls, type) and issubclass(event_cls, Event)):
            raise TypeError(
                f"event_cls must be an Event subclass; got {event_cls!r}"
            )
        if not override and event_cls in self._policies:
            raise OutboundPolicyConflict(
                f"Outbound policy for {event_cls.__name__} is already "
                "registered; pass override=True to replace it."
            )
        self._policies[event_cls] = policy

    def has_policy(self, event_cls: type[Event]) -> bool:
        """True if ``event_cls`` has a registered outbound policy."""
        return event_cls in self._policies

    def apply(self, event: Event, client_ctx: Any) -> bool:
        """Return whether ``event`` may be pushed to ``client_ctx``.

        Looks up the policy by ``type(event)`` (exact-type routing, per
        ADR-0007 — subclasses do not inherit a parent's policy) and
        invokes it with ``(event, client_ctx)``.

        **Deny-by-default for unregistered Event classes.** If no policy
        is registered for ``type(event)``, this method returns ``False``.
        Forgetting to register a policy is a programmer error, but the
        safe failure mode is to drop the message rather than leak it —
        the SSE layer should already have rejected the subscription
        up-front, so reaching this branch at runtime indicates an Event
        was published without an outbound contract; deny silently here
        and let observability tooling catch the missing-policy case.

        Pinned in the test suite — change of this default requires a
        deliberate test update.
        """
        policy = self._policies.get(type(event))
        if policy is None:
            return False
        return policy(event, client_ctx)


__all__ = [
    "OUTBOUND_POLICY_ATTR",
    "OutboundPolicy",
    "OutboundPolicyRegistry",
    "outbound_policy",
]
