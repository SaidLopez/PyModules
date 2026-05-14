"""Manifest endpoint serving OpenAPI + AsyncAPI (issue #6 / ADR-0009).

This module ships the third leg of the full-stack v1 contract: a single
HTTP endpoint that returns the host's complete machine-readable surface
in one document — FastAPI's ``app.openapi()`` for Commands plus
:func:`pymodules.contrib.fullstack.asyncapi.emit_asyncapi` for declared
Events. Codegen tooling fetches this once, build- or runtime, to keep
the JS-side types in lock-step with the backend.

What this module ships
----------------------

- :func:`build_manifest_router` — a factory returning a FastAPI
  ``APIRouter`` with one ``GET`` route (default
  ``/__pymodules__/manifest``) whose response shape is::

      {
          "openapi":  <fastapi_app.openapi()>,
          "asyncapi": <emit_asyncapi(host)>,
      }

  The factory caches the computed document on a closure variable; the
  cache is built lazily on the first request, not at host startup, so
  the router can be wired into the app before all Modules have
  registered. Subsequent requests are served from the cache without
  re-walking the host.

  The factory also exposes the cache-clear callable on the returned
  router as the attribute ``invalidate``. Callers that want to wire
  cache invalidation into their own lifecycle (e.g. hot-reload tooling)
  can call ``router.invalidate()`` directly.

- :func:`attach_manifest_cache_invalidator` — a small helper that
  monkey-patches ``host.register`` so each Module (re-)registration
  clears the manifest cache. We pick this approach (rather than asking
  ``pymodules.host`` to grow a registration-hook surface for the sake
  of one contrib module) because:

    1. It keeps the cache-invalidation seam *in the contrib package
       that owns the cache*, not in core. ADR-0002 keeps core
       standalone.
    2. ``host.register`` is the natural seam — by the time it returns,
       the new Module's ``published_events`` are visible to
       :func:`emit_asyncapi`, so invalidating *after* registration is
       always correct.
    3. The patch is additive — original behaviour, including the
       rollback path on registration failure, is preserved verbatim.
       The cache is only cleared on a *successful* registration (we
       invalidate after the wrapped call returns, before propagating
       its result).

  A single host should only have the patch attached once; calling
  :func:`attach_manifest_cache_invalidator` twice on the same host
  would chain two invalidators, which is harmless but wasteful. We
  guard against double-attachment with a marker attribute on the host.

Auth
----

The route depends on the cookie auth shim
(:func:`make_cookie_auth_dependency`). Codegen tools authenticate as a
developer using the same access cookie that browser clients use. An
unauthenticated request returns 401 with
``WWW-Authenticate: Cookie realm="pymodules"`` (the shim raises an
``HTTPException`` with that header; FastAPI surfaces it untouched).

The caller passes a ready-made dependency via the
``cookie_auth_dependency`` parameter. This keeps the factory free of
any direct ``AuthProvider`` knowledge — the same shim instance can be
shared with the SSE endpoint (#7), the refresh router, and the host's
other cookie-protected surfaces.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, FastAPI

from .asyncapi import emit_asyncapi

if TYPE_CHECKING:
    from pymodules.host import ModuleHost


# Default path the manifest is mounted at. Pinned per the PRD / ADR-0009;
# overridable per-host via the ``path`` factory argument.
DEFAULT_MANIFEST_PATH = "/__pymodules__/manifest"

# Marker attribute written on a ``ModuleHost`` by
# :func:`attach_manifest_cache_invalidator` so a second invocation on the
# same host is a no-op rather than chaining a second invalidator on top
# of the first. The name is namespaced so it cannot collide with anything
# core or another contrib writes.
_INVALIDATOR_ATTACHED_ATTR = "__pymodules_manifest_invalidator_attached__"


def build_manifest_router(
    host: ModuleHost,
    fastapi_app: FastAPI,
    *,
    cookie_auth_dependency: Callable[..., Any],
    path: str = DEFAULT_MANIFEST_PATH,
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` exposing the combined manifest.

    Args:
        host: The :class:`ModuleHost` whose declared Events feed
            :func:`emit_asyncapi`.
        fastapi_app: The FastAPI app whose ``app.openapi()`` provides the
            Command surface. Passed explicitly (rather than discovered
            via the host) because the host has no static handle on the
            app — they are wired together by user code.
        cookie_auth_dependency: A FastAPI dependency callable (typically
            the result of :func:`make_cookie_auth_dependency`) that
            authenticates the caller via the access cookie. Passed in
            rather than constructed here so the same shim instance can
            be shared across the manifest, SSE, and refresh surfaces.
        path: Path the manifest is mounted at. Defaults to
            ``"/__pymodules__/manifest"`` per the PRD.

    Returns:
        An ``APIRouter`` with a single ``GET`` route. Mount it with
        ``app.include_router(router)``. The returned router carries an
        ``invalidate`` attribute — a zero-arg callable that clears the
        cache so the next request recomputes.
    """
    # Closure cache. ``None`` means "not yet computed"; once populated
    # the dict is returned by reference on every cache hit. We never
    # mutate the cached dict — :func:`emit_asyncapi` and
    # ``fastapi_app.openapi()`` both return fresh dicts per call, so the
    # cached value is owned by this closure for its lifetime.
    cache: dict[str, dict[str, Any] | None] = {"value": None}

    def invalidate() -> None:
        """Clear the cache so the next request recomputes the manifest."""
        cache["value"] = None

    def _compute() -> dict[str, Any]:
        """Build the manifest document fresh.

        Kept inline rather than as a module-level helper because the
        OpenAPI doc is derived from the bound ``fastapi_app`` and the
        AsyncAPI doc from the bound ``host`` — both closure-captured.
        """
        return {
            "openapi": fastapi_app.openapi(),
            "asyncapi": emit_asyncapi(host),
        }

    router = APIRouter()

    @router.get(path)
    async def get_manifest(
        # The cookie auth dependency's return value (``ClientContext``)
        # is unused here — we only need it to enforce the 401 challenge
        # on missing/expired cookies. Naming it ``_client`` rather than
        # discarding it keeps the dependency wired into FastAPI's
        # signature inspection (a bare ``Depends(...)`` with no name
        # would still work, but the convention across this contrib is
        # to bind a name).
        _client: Any = Depends(cookie_auth_dependency),
    ) -> dict[str, Any]:
        cached = cache["value"]
        if cached is None:
            cached = _compute()
            cache["value"] = cached
        return cached

    # Expose the invalidator on the router so callers (including
    # ``attach_manifest_cache_invalidator`` below) can clear the cache
    # without needing a closure reference. ``APIRouter`` is an ordinary
    # Python object; setattr is fine.
    router.invalidate = invalidate  # type: ignore[attr-defined]
    return router


def attach_manifest_cache_invalidator(
    host: ModuleHost,
    invalidate: Callable[[], None],
) -> None:
    """Wire ``invalidate`` into ``host.register`` so re-registration clears the cache.

    Args:
        host: The :class:`ModuleHost` whose registrations should trigger
            cache invalidation.
        invalidate: A zero-arg callable that clears the manifest cache.
            Typically the ``invalidate`` attribute exposed on the router
            returned by :func:`build_manifest_router`.

    How it works
    ------------

    We replace ``host.register`` with a thin wrapper that delegates to
    the original, then calls ``invalidate()`` on success. Failure paths
    (``DuplicateCommandError``, ``OutboundPolicyConflict``,
    ``ModuleRegistrationError``) propagate unchanged — and notably, we
    do **not** invalidate on failure: a rolled-back registration leaves
    the manifest cache valid because the host's Module list is
    unchanged.

    Why a monkey-patch rather than a host hook
    -------------------------------------------

    Adding a ``host.on_register`` callback surface to core for the
    benefit of one contrib module would push manifest-specific concern
    into core (against ADR-0002). The seam stays in the contrib package
    that owns the cache.

    Idempotency
    -----------

    Calling this helper twice on the same host is a no-op the second
    time. We mark the host with a private attribute on first attach;
    subsequent calls see the marker and return early. This matters
    because a developer might call ``build_manifest_router`` more than
    once in tests; we don't want each call to stack another wrapper.
    """
    if getattr(host, _INVALIDATOR_ATTACHED_ATTR, False):
        return

    original_register = host.register

    def register_with_invalidation(*args: Any, **kwargs: Any) -> Any:
        # Delegate first so a failed registration (and its rollback)
        # never triggers a spurious invalidation. ``original_register``
        # returns ``host`` for chaining; we propagate that as-is.
        result = original_register(*args, **kwargs)
        invalidate()
        return result

    # ``host.register`` is a bound method on the instance; replacing
    # the attribute on ``host`` itself (not the class) keeps other
    # ModuleHost instances unaffected.
    host.register = register_with_invalidation  # type: ignore[method-assign]
    setattr(host, _INVALIDATOR_ATTACHED_ATTR, True)


__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "attach_manifest_cache_invalidator",
    "build_manifest_router",
]
