"""Tests for the manifest endpoint (issue #6).

Covers the acceptance criteria pinned on ``gh issue 6``:

- ``GET /__pymodules__/manifest`` returns
  ``{"openapi": <fastapi_app.openapi()>, "asyncapi": <emit_asyncapi(host)>}``
  with both subdocuments populated.
- The path is configurable; a non-default path is also served.
- The manifest is cached after first build; re-registering a Module
  invalidates the cache so the next response reflects the new
  ``published_events``.
- Unauthenticated request (no cookie) → 401 with
  ``WWW-Authenticate: Cookie realm="pymodules"``.

Test style mirrors ``tests/contrib/fullstack/test_cookie_auth.py``:
a real ``JWTAuthProvider`` fixture, a ``TestClient`` per case, and
inline dataclass Events / Modules so each test reads top-to-bottom.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymodules import Event, Module, ModuleHost
from pymodules.contrib.fullstack import (
    attach_manifest_cache_invalidator,
    build_manifest_router,
    make_cookie_auth_dependency,
)

# ---------------------------------------------------------------------------
# Synthetic Events / Modules
# ---------------------------------------------------------------------------


@dataclass
class WidgetCreated(Event):
    """Event the initial Module publishes."""

    widget_id: str = ""
    name: str = "widget.created"


@dataclass
class GadgetCreated(Event):
    """Event a *second* Module publishes, used to prove cache invalidation."""

    gadget_id: str = ""
    name: str = "gadget.created"


class WidgetModule(Module):
    published_events = (WidgetCreated,)


class GadgetModule(Module):
    published_events = (GadgetCreated,)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_provider():
    """A real ``JWTAuthProvider`` so we exercise the cookie auth path end-to-end."""
    from pymodules.contrib.api.auth import JWTAuthProvider, JWTSettings

    settings = JWTSettings(
        secret_key="test-secret-key-for-manifest-tests-1234567890",
        access_token_expire_minutes=30,
    )
    return JWTAuthProvider(settings)


@pytest.fixture
def issue_token(jwt_provider):
    """Sync helper that issues a JWT with the given claims for TestClient."""

    def _issue(claims: dict[str, Any]) -> str:
        return asyncio.get_event_loop().run_until_complete(jwt_provider.create_token(claims))

    return _issue


def _build_app(
    host: ModuleHost,
    jwt_provider,
    *,
    path: str | None = None,
) -> tuple[FastAPI, Any]:
    """Wire a FastAPI app with the manifest router and return ``(app, invalidate)``.

    The returned ``invalidate`` callable is the router's cache-clear hook,
    surfaced for tests that exercise invalidation directly.
    """
    app = FastAPI()
    cookie_auth = make_cookie_auth_dependency(jwt_provider)
    if path is None:
        router = build_manifest_router(host, app, cookie_auth_dependency=cookie_auth)
    else:
        router = build_manifest_router(host, app, cookie_auth_dependency=cookie_auth, path=path)
    app.include_router(router)
    return app, router.invalidate  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestManifestHappyPath:
    """Authenticated GET returns both subdocuments populated."""

    def test_returns_openapi_and_asyncapi_subdocs(self, jwt_provider, issue_token) -> None:
        host = ModuleHost()
        host.register(WidgetModule())

        app, _invalidate = _build_app(host, jwt_provider)
        token = issue_token({"sub": "dev-1"})
        client = TestClient(app)

        response = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )

        assert response.status_code == 200
        body = response.json()
        # Both top-level keys must be present and dict-shaped.
        assert set(body.keys()) == {"openapi", "asyncapi"}
        assert isinstance(body["openapi"], dict)
        assert isinstance(body["asyncapi"], dict)

        # OpenAPI subdoc carries FastAPI's standard top-level keys.
        assert body["openapi"].get("openapi", "").startswith("3.")
        assert "info" in body["openapi"]
        assert "paths" in body["openapi"]

        # AsyncAPI subdoc carries the declared Event as a channel.
        assert body["asyncapi"].get("asyncapi") == "3.0.0"
        assert "WidgetCreated" in body["asyncapi"]["channels"]


# ---------------------------------------------------------------------------
# Cache invalidation on Module re-registration
# ---------------------------------------------------------------------------


class TestManifestCacheInvalidation:
    """Re-registering a Module invalidates the cache for the next request."""

    def test_new_module_after_first_fetch_reflected_after_invalidator_attached(
        self, jwt_provider, issue_token
    ) -> None:
        host = ModuleHost()
        host.register(WidgetModule())

        app, invalidate = _build_app(host, jwt_provider)
        # Wire the host-registration hook so future ``host.register``
        # calls clear the cache automatically. This is the documented
        # production path (see ``attach_manifest_cache_invalidator``).
        attach_manifest_cache_invalidator(host, invalidate)

        token = issue_token({"sub": "dev-1"})
        client = TestClient(app)

        first = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )
        assert first.status_code == 200
        first_channels = set(first.json()["asyncapi"]["channels"].keys())
        assert first_channels == {"WidgetCreated"}

        # Register a second Module declaring a *different* Event. The
        # invalidator must have cleared the cache on the way out of
        # ``host.register``, so the next GET reflects the new state.
        host.register(GadgetModule())

        second = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )
        assert second.status_code == 200
        second_channels = set(second.json()["asyncapi"]["channels"].keys())
        assert second_channels == {"WidgetCreated", "GadgetCreated"}

    def test_invalidate_attribute_clears_cache_directly(self, jwt_provider, issue_token) -> None:
        """The ``router.invalidate`` callable also clears the cache on its own.

        This exercises the manual-invalidation surface for callers that
        choose not to use ``attach_manifest_cache_invalidator``.
        """
        host = ModuleHost()
        host.register(WidgetModule())

        app, invalidate = _build_app(host, jwt_provider)
        token = issue_token({"sub": "dev-1"})
        client = TestClient(app)

        first = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )
        assert first.status_code == 200
        assert set(first.json()["asyncapi"]["channels"].keys()) == {"WidgetCreated"}

        # Register without the host hook → cache would still serve the
        # stale doc on its own.
        host.register(GadgetModule())
        # Then manually invalidate, mirroring what hot-reload tooling
        # would do at the boundary of its lifecycle.
        invalidate()

        third = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )
        assert set(third.json()["asyncapi"]["channels"].keys()) == {
            "WidgetCreated",
            "GadgetCreated",
        }


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


class TestManifestAuth:
    """Cookie auth shim enforces the 401 challenge on the route."""

    def test_no_cookie_returns_401_with_challenge(self, jwt_provider) -> None:
        host = ModuleHost()
        host.register(WidgetModule())

        app, _invalidate = _build_app(host, jwt_provider)
        client = TestClient(app)

        response = client.get("/__pymodules__/manifest")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'


# ---------------------------------------------------------------------------
# Configurable path
# ---------------------------------------------------------------------------


class TestManifestPath:
    """A non-default mount path also serves the manifest."""

    def test_custom_path_is_served(self, jwt_provider, issue_token) -> None:
        host = ModuleHost()
        host.register(WidgetModule())

        app, _invalidate = _build_app(host, jwt_provider, path="/custom/manifest")
        token = issue_token({"sub": "dev-1"})
        client = TestClient(app)

        # Default path no longer responds (no route was mounted there).
        default = client.get(
            "/__pymodules__/manifest",
            cookies={"pymodules_access": token},
        )
        assert default.status_code == 404

        # Custom path serves the manifest.
        custom = client.get(
            "/custom/manifest",
            cookies={"pymodules_access": token},
        )
        assert custom.status_code == 200
        body = custom.json()
        assert set(body.keys()) == {"openapi", "asyncapi"}
        assert "WidgetCreated" in body["asyncapi"]["channels"]
