"""Exception hierarchy for ``pymodules.contrib.fullstack``.

This module owns the root of the fullstack contrib's exception tree. All
errors raised from anywhere under ``pymodules.contrib.fullstack`` inherit
from :class:`FullstackError`, which itself inherits from the framework's
:class:`pymodules.exceptions.PyModulesError` base — so a caller's
``except PyModulesError:`` block catches fullstack errors uniformly with
core errors.

This first slice ships :class:`OutboundPolicyConflict`. Subsequent slices
(#5 cookie auth, #6 SSE endpoint, #7 manifest endpoint) will add their own
concrete subclasses here (e.g. ``UnknownEventSubscription``,
``MissingOutboundPolicy``). Keeping every fullstack error rooted in
:class:`FullstackError` lets the SSE / cookie code share one ``except``
clause when surfacing fullstack-specific failures to FastAPI.
"""

from __future__ import annotations

from pymodules.exceptions import PyModulesError


class FullstackError(PyModulesError):
    """Base class for every error raised from ``pymodules.contrib.fullstack``.

    Subclass this for any new fullstack-specific failure. Catching
    ``FullstackError`` lets calling code (e.g. FastAPI exception handlers
    in the SSE / manifest slices) treat all contrib-fullstack failures
    uniformly, while still letting ``except PyModulesError`` catch them
    alongside core framework errors.
    """


class OutboundPolicyConflict(FullstackError):
    """Raised when an Outbound policy is registered twice for the same Event.

    The :class:`pymodules.contrib.fullstack.outbound_policy.OutboundPolicyRegistry`
    rejects a second ``register(EventCls, policy)`` call for an Event class
    that already has a policy unless ``override=True`` is passed. This
    mirrors the existing duplicate-Command-claim guard
    (:class:`pymodules.exceptions.DuplicateCommandError`): silent
    last-writer-wins on an Event's outbound filter would be a cross-tenant
    leakage footgun, so the framework defaults to loud rejection.
    """


class UnknownEventSubscription(FullstackError):
    """Raised when an SSE subscription names an Event class the host doesn't know.

    The SSE endpoint (:mod:`pymodules.contrib.fullstack.sse`) resolves each
    name in the ``?subscribe=`` query parameter against the host's set of
    declared ``published_events``. A name that resolves to nothing is a
    programmer error in the calling JS layer (typo, stale build, missing
    Module registration). The endpoint surfaces this as HTTP 400 with body
    ``{"error": "unknown_event", "event": "<name>"}`` so the failure is
    found at subscribe time rather than silently dropped at publish time.
    """


class MissingOutboundPolicy(FullstackError):
    """Raised when an SSE subscription names a known Event with no Outbound policy.

    Deny-by-default: an Event class that no Module has wired an
    ``@outbound_policy`` callable for cannot reach the browser. The SSE
    endpoint rejects the subscription up-front with HTTP 400 and body
    ``{"error": "no_outbound_policy", "event": "<name>"}`` so a missing
    policy fails loud at subscribe time rather than masking a
    cross-tenant leakage bug at runtime.
    """


__all__ = [
    "FullstackError",
    "OutboundPolicyConflict",
    "UnknownEventSubscription",
    "MissingOutboundPolicy",
]
