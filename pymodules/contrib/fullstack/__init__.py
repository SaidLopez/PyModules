"""Full-stack contrib for PyModules.

This contrib package owns the backend half of the full-stack story locked in
ADR-0009: AsyncAPI emission for published **Events**, an SSE push channel for
browser clients, a deny-by-default **Outbound policy** registry, a manifest
endpoint combining OpenAPI + AsyncAPI, and a cookie auth shim that delegates
to the existing ``pymodules.contrib.api.auth`` ``AuthMiddleware``.

This file is intentionally near-empty: the contrib is import-isolated from
core (per ADR-0002) and from sibling contribs. Importing
``pymodules.contrib.fullstack`` pulls in nothing beyond what each re-exported
symbol strictly needs — and each symbol's submodule is loaded lazily via
``__getattr__`` so unused features stay un-imported.

This first slice ships only ``emit_asyncapi``. SSE, Outbound policy, manifest
endpoint, and the cookie shim land in subsequent slices.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .asyncapi import emit_asyncapi
    from .exceptions import FullstackError, OutboundPolicyConflict
    from .outbound_policy import OutboundPolicyRegistry, outbound_policy

    # --- Cookie auth shim (issue #5) --------------------------------------
    from .client_context import ClientContext
    from .cookie_auth import (
        DEFAULT_ACCESS_COOKIE,
        DEFAULT_REFRESH_COOKIE,
        build_refresh_router,
        make_cookie_auth_dependency,
    )

    # --- Manifest endpoint (issue #6) -------------------------------------
    from .manifest import (
        DEFAULT_MANIFEST_PATH,
        attach_manifest_cache_invalidator,
        build_manifest_router,
    )

    # --- SSE push endpoint (issue #7) -------------------------------------
    from .exceptions import MissingOutboundPolicy, UnknownEventSubscription
    from .sse import build_sse_router

    # --- SSE observability + graceful shutdown drain (issue #8) -----------
    from .sse import SSEMetrics, register_with_host

__all__ = [
    "emit_asyncapi",
    # --- Outbound policy (issue #4) ---------------------------------------
    "FullstackError",
    "OutboundPolicyConflict",
    "OutboundPolicyRegistry",
    "outbound_policy",
    # --- Cookie auth shim (issue #5) --------------------------------------
    "ClientContext",
    "DEFAULT_ACCESS_COOKIE",
    "DEFAULT_REFRESH_COOKIE",
    "build_refresh_router",
    "make_cookie_auth_dependency",
    # --- Manifest endpoint (issue #6) -------------------------------------
    "DEFAULT_MANIFEST_PATH",
    "attach_manifest_cache_invalidator",
    "build_manifest_router",
    # --- SSE push endpoint (issue #7) -------------------------------------
    "MissingOutboundPolicy",
    "UnknownEventSubscription",
    "build_sse_router",
    # --- SSE observability + graceful shutdown drain (issue #8) -----------
    "SSEMetrics",
    "register_with_host",
]


def __getattr__(name: str) -> Any:
    """Lazy-load fullstack components on first attribute access."""
    if name == "emit_asyncapi":
        from .asyncapi import emit_asyncapi

        return emit_asyncapi
    # --- Outbound policy (issue #4) ---------------------------------------
    # Kept as a clearly-separated block so concurrent edits from sibling
    # slices (#5 cookie auth, #6 SSE) drop in alongside without conflict.
    if name in ("FullstackError", "OutboundPolicyConflict"):
        from . import exceptions

        return getattr(exceptions, name)
    if name in ("OutboundPolicyRegistry", "outbound_policy"):
        # The submodule ``pymodules.contrib.fullstack.outbound_policy``
        # exports a function also named ``outbound_policy``. Python's
        # import machinery binds the submodule onto the package's
        # ``__dict__`` as the attribute ``outbound_policy``; we then
        # *override* that binding with the decorator function so a
        # subsequent ``from pymodules.contrib.fullstack import
        # outbound_policy`` resolves to the function, not the module.
        # We also use ``importlib.import_module`` (rather than ``from
        # . import outbound_policy``) because the latter would re-enter
        # this very ``__getattr__`` and recurse.
        _op = importlib.import_module(".outbound_policy", __name__)
        # Rebind both names onto the package namespace so they are
        # cached for subsequent lookups (bypassing ``__getattr__``
        # entirely) and resolve to the function, not the submodule.
        import sys as _sys

        _pkg = _sys.modules[__name__]
        _pkg.OutboundPolicyRegistry = _op.OutboundPolicyRegistry  # type: ignore[attr-defined]
        _pkg.outbound_policy = _op.outbound_policy  # type: ignore[attr-defined]
        return getattr(_op, name)
    # ----------------------------------------------------------------------
    # --- Cookie auth shim (issue #5) --------------------------------------
    # Separated block; pulls in FastAPI only on first access, keeping the
    # contrib import-cheap for callers that only want ``emit_asyncapi`` or
    # the outbound policy registry.
    if name == "ClientContext":
        from .client_context import ClientContext

        return ClientContext
    if name in (
        "DEFAULT_ACCESS_COOKIE",
        "DEFAULT_REFRESH_COOKIE",
        "build_refresh_router",
        "make_cookie_auth_dependency",
    ):
        from . import cookie_auth as _cookie_auth

        return getattr(_cookie_auth, name)
    # ----------------------------------------------------------------------
    # --- Manifest endpoint (issue #6) -------------------------------------
    # Self-contained block; pulls in FastAPI only on first access. Wires
    # the OpenAPI + AsyncAPI manifest route + the cache-invalidation
    # helper that hooks ``host.register``.
    if name in (
        "DEFAULT_MANIFEST_PATH",
        "attach_manifest_cache_invalidator",
        "build_manifest_router",
    ):
        from . import manifest as _manifest

        return getattr(_manifest, name)
    # ----------------------------------------------------------------------
    # --- SSE push endpoint (issue #7) -------------------------------------
    # Self-contained block. The two new exceptions live in ``exceptions``
    # alongside ``FullstackError`` / ``OutboundPolicyConflict``; the router
    # factory is in ``sse`` and pulls in FastAPI + ``httpx-sse``-friendly
    # streaming only on first access.
    if name in ("UnknownEventSubscription", "MissingOutboundPolicy"):
        from . import exceptions

        return getattr(exceptions, name)
    if name == "build_sse_router":
        from .sse import build_sse_router

        return build_sse_router
    # ----------------------------------------------------------------------
    # --- SSE observability + graceful shutdown drain (issue #8) -----------
    # Same submodule (``sse``) as build_sse_router; kept as a separate
    # ``__getattr__`` block so concurrent slices touching #7 vs #8 don't
    # collide on the same line.
    if name in ("SSEMetrics", "register_with_host"):
        from . import sse as _sse

        return getattr(_sse, name)
    # ----------------------------------------------------------------------
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
