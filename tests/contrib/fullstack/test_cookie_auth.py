"""Tests for the cookie auth shim (issue #5).

Covers the acceptance criteria pinned on ``gh issue 5``:

- Valid access cookie → ``ClientContext`` produced; protected route 200.
- Expired access cookie → 401 with ``WWW-Authenticate: Cookie realm="pymodules"``.
- Missing access cookie → 401 with the same challenge header.
- ``/__pymodules__/auth/refresh`` happy path: both cookies rotated, 200.
- Refresh with invalid token → 401.
- Bearer-token regression: ``AuthMiddleware`` still works on non-cookie routes.
- Cookie attributes in production config: ``HttpOnly``, ``SameSite=Strict``,
  ``Secure``.

Test style mirrors ``tests/api/auth/test_middleware.py`` (which uses
``fastapi.testclient.TestClient`` + a ``JWTAuthProvider`` fixture).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_provider():
    """A real ``JWTAuthProvider`` so we exercise the production decode path."""
    from pymodules.contrib.api.auth import JWTAuthProvider, JWTSettings

    settings = JWTSettings(
        secret_key="test-secret-key-for-cookie-auth-tests-123456",
        access_token_expire_minutes=30,
    )
    return JWTAuthProvider(settings)


@pytest.fixture
def issue_token(jwt_provider):
    """Helper that issues a JWT with the given claims (sync, for TestClient)."""

    async def _issue(claims: dict[str, Any]) -> str:
        return await jwt_provider.create_token(claims)

    import asyncio

    def _sync_issue(claims: dict[str, Any]) -> str:
        return asyncio.get_event_loop().run_until_complete(_issue(claims))

    return _sync_issue


def _make_expired_jwt(provider, claims: dict[str, Any]) -> str:
    """Hand-build a JWT whose ``exp`` is already in the past.

    ``JWTAuthProvider.create_token`` always stamps ``exp`` in the future, so
    we encode directly through ``python-jose`` using the provider's secret
    to produce an authentically-signed-but-expired token.
    """
    from jose import jwt as _jose_jwt

    payload = {
        **claims,
        "iat": datetime.now(UTC) - timedelta(hours=2),
        "exp": datetime.now(UTC) - timedelta(hours=1),
    }
    return _jose_jwt.encode(
        payload,
        provider.settings.secret_key,
        algorithm=provider.settings.algorithm,
    )


# ---------------------------------------------------------------------------
# ClientContext shape
# ---------------------------------------------------------------------------


class TestClientContext:
    """``ClientContext`` is a frozen identity dataclass — not CommandContext."""

    def test_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from pymodules.contrib.fullstack import ClientContext

        ctx = ClientContext(user_id="u", tenant_id="t", claims={})
        with pytest.raises(FrozenInstanceError):
            ctx.user_id = "other"  # type: ignore[misc]

    def test_distinct_from_command_context(self) -> None:
        """ADR-0006: ClientContext must not be the same type as CommandContext."""
        from pymodules.contrib.fullstack import ClientContext
        from pymodules.interfaces import CommandContext

        assert ClientContext is not CommandContext

    def test_tenant_id_optional(self) -> None:
        from pymodules.contrib.fullstack import ClientContext

        ctx = ClientContext(user_id="u")
        assert ctx.tenant_id is None
        assert ctx.claims == {}


# ---------------------------------------------------------------------------
# Cookie access dependency
# ---------------------------------------------------------------------------


class TestCookieAuthDependency:
    """The ``cookie_auth_required`` FastAPI dependency."""

    def test_valid_access_cookie_returns_client_context(self, jwt_provider, issue_token) -> None:
        """Valid access cookie -> ClientContext threaded into the handler."""
        from pymodules.contrib.fullstack import (
            ClientContext,
            make_cookie_auth_dependency,
        )

        cookie_auth = make_cookie_auth_dependency(jwt_provider)
        app = FastAPI()

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict[str, Any]:
            return {
                "user_id": client.user_id,
                "tenant_id": client.tenant_id,
                "claims_has_sub": "sub" in client.claims,
            }

        token = issue_token({"sub": "user-42", "tenant_id": "acme"})
        client = TestClient(app)
        response = client.get("/me", cookies={"pymodules_access": token})

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "user_id": "user-42",
            "tenant_id": "acme",
            "claims_has_sub": True,
        }

    def test_missing_access_cookie_returns_401_with_challenge(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import (
            ClientContext,
            make_cookie_auth_dependency,
        )

        cookie_auth = make_cookie_auth_dependency(jwt_provider)
        app = FastAPI()

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict[str, Any]:
            return {"user_id": client.user_id}

        client = TestClient(app)
        response = client.get("/me")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_expired_access_cookie_returns_401_with_challenge(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import (
            ClientContext,
            make_cookie_auth_dependency,
        )

        cookie_auth = make_cookie_auth_dependency(jwt_provider)
        app = FastAPI()

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict[str, Any]:
            return {"user_id": client.user_id}

        expired = _make_expired_jwt(jwt_provider, {"sub": "user-42"})
        client = TestClient(app)
        response = client.get("/me", cookies={"pymodules_access": expired})

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_invalid_access_cookie_returns_401(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import (
            ClientContext,
            make_cookie_auth_dependency,
        )

        cookie_auth = make_cookie_auth_dependency(jwt_provider)
        app = FastAPI()

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict[str, Any]:
            return {"user_id": client.user_id}

        client = TestClient(app)
        response = client.get("/me", cookies={"pymodules_access": "not-a-real-jwt"})

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_configurable_cookie_name(self, jwt_provider, issue_token) -> None:
        """A custom cookie name reads from a different cookie key."""
        from pymodules.contrib.fullstack import (
            ClientContext,
            make_cookie_auth_dependency,
        )

        cookie_auth = make_cookie_auth_dependency(jwt_provider, access_cookie_name="my_app_access")
        app = FastAPI()

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict[str, Any]:
            return {"user_id": client.user_id}

        token = issue_token({"sub": "user-42"})
        client = TestClient(app)

        # Wrong cookie name -> 401
        wrong = client.get("/me", cookies={"pymodules_access": token})
        assert wrong.status_code == 401

        # Right cookie name -> 200
        right = client.get("/me", cookies={"my_app_access": token})
        assert right.status_code == 200


# ---------------------------------------------------------------------------
# /__pymodules__/auth/refresh endpoint
# ---------------------------------------------------------------------------


class TestRefreshEndpoint:
    """The ``/__pymodules__/auth/refresh`` endpoint."""

    def test_valid_refresh_rotates_both_cookies(self, jwt_provider, issue_token) -> None:
        from pymodules.contrib.fullstack import build_refresh_router

        # ``secure=False`` because TestClient is plain HTTP; the
        # production-config attributes test below pins ``secure=True``.
        router = build_refresh_router(jwt_provider, secure=False)
        app = FastAPI()
        app.include_router(router)

        refresh_token = issue_token({"sub": "user-42", "tenant_id": "acme"})
        client = TestClient(app)

        response = client.post(
            "/__pymodules__/auth/refresh",
            cookies={"pymodules_refresh": refresh_token},
        )

        assert response.status_code == 200
        assert response.json() == {"refreshed": True}

        # Both cookies were re-set on the response.
        set_cookie_headers = response.headers.get_list("set-cookie")
        access_cookies = [h for h in set_cookie_headers if h.startswith("pymodules_access=")]
        refresh_cookies = [h for h in set_cookie_headers if h.startswith("pymodules_refresh=")]
        assert len(access_cookies) == 1
        assert len(refresh_cookies) == 1

        # The new tokens are present in the test client's cookie jar and
        # validate against the same provider.
        new_access = client.cookies.get("pymodules_access")
        new_refresh = client.cookies.get("pymodules_refresh")
        assert new_access is not None
        assert new_refresh is not None
        assert new_access != refresh_token
        assert new_refresh != refresh_token

    def test_invalid_refresh_returns_401(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import build_refresh_router

        router = build_refresh_router(jwt_provider, secure=False)
        app = FastAPI()
        app.include_router(router)

        client = TestClient(app)
        response = client.post(
            "/__pymodules__/auth/refresh",
            cookies={"pymodules_refresh": "not-a-jwt"},
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_missing_refresh_returns_401(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import build_refresh_router

        router = build_refresh_router(jwt_provider, secure=False)
        app = FastAPI()
        app.include_router(router)

        client = TestClient(app)
        response = client.post("/__pymodules__/auth/refresh")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_expired_refresh_returns_401(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import build_refresh_router

        router = build_refresh_router(jwt_provider, secure=False)
        app = FastAPI()
        app.include_router(router)

        expired = _make_expired_jwt(jwt_provider, {"sub": "user-42"})
        client = TestClient(app)
        response = client.post(
            "/__pymodules__/auth/refresh",
            cookies={"pymodules_refresh": expired},
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'

    def test_production_config_cookie_attributes(self, jwt_provider, issue_token) -> None:
        """HttpOnly + SameSite=Strict + Secure all set when ``secure=True``."""
        from pymodules.contrib.fullstack import build_refresh_router

        router = build_refresh_router(jwt_provider, secure=True)
        app = FastAPI()
        app.include_router(router)

        refresh_token = issue_token({"sub": "user-42"})
        client = TestClient(app)
        response = client.post(
            "/__pymodules__/auth/refresh",
            cookies={"pymodules_refresh": refresh_token},
        )

        assert response.status_code == 200

        set_cookie_headers = response.headers.get_list("set-cookie")
        access_headers = [h for h in set_cookie_headers if h.startswith("pymodules_access=")]
        refresh_headers = [h for h in set_cookie_headers if h.startswith("pymodules_refresh=")]
        assert access_headers
        assert refresh_headers

        for header in (*access_headers, *refresh_headers):
            lowered = header.lower()
            assert "httponly" in lowered, f"HttpOnly missing in {header!r}"
            assert "samesite=strict" in lowered, f"SameSite=Strict missing in {header!r}"
            assert "secure" in lowered, f"Secure missing in {header!r}"

        # Refresh cookie is path-scoped to the refresh endpoint so other
        # handlers never receive it.
        assert "path=/__pymodules__/auth/refresh" in refresh_headers[0].lower()

    def test_configurable_cookie_names(self, jwt_provider, issue_token) -> None:
        from pymodules.contrib.fullstack import build_refresh_router

        router = build_refresh_router(
            jwt_provider,
            access_cookie_name="ax",
            refresh_cookie_name="rx",
            secure=False,
        )
        app = FastAPI()
        app.include_router(router)

        refresh_token = issue_token({"sub": "user-42"})
        client = TestClient(app)
        response = client.post(
            "/__pymodules__/auth/refresh",
            cookies={"rx": refresh_token},
        )

        assert response.status_code == 200
        set_cookies = response.headers.get_list("set-cookie")
        assert any(h.startswith("ax=") for h in set_cookies)
        assert any(h.startswith("rx=") for h in set_cookies)


# ---------------------------------------------------------------------------
# Bearer-token regression
# ---------------------------------------------------------------------------


class TestBearerTokenRegression:
    """ADR-0002: existing ``AuthMiddleware`` path is untouched by the shim."""

    def test_bearer_route_still_works_without_cookie(self, jwt_provider, issue_token) -> None:
        """A route guarded only by ``AuthMiddleware`` authenticates via the
        ``Authorization: Bearer`` header — no cookie involved, no cookie shim
        imported on the request path."""
        from pymodules.contrib.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=jwt_provider)

        @app.get("/bearer-only")
        def bearer_only() -> dict[str, str]:
            return {"status": "ok"}

        token = issue_token({"sub": "user-42"})
        client = TestClient(app)

        # With bearer header -> 200.
        ok = client.get("/bearer-only", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert ok.json() == {"status": "ok"}

        # Without bearer header -> 401 (existing AuthMiddleware behaviour).
        unauth = client.get("/bearer-only")
        assert unauth.status_code == 401

        # And critically: passing only a cookie (no Authorization header) is
        # still rejected — the bearer path doesn't accidentally inherit the
        # cookie shim's read behaviour.
        cookie_only = client.get("/bearer-only", cookies={"pymodules_access": token})
        assert cookie_only.status_code == 401
