"""Tests for auth middleware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request


class TestAuthMiddleware:
    """Tests for AuthMiddleware."""

    @pytest.fixture
    def mock_provider(self):
        """Create a mock auth provider."""
        from pymodules.api.auth import AuthProvider, TokenClaims

        class MockProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token == "valid-token":
                    return TokenClaims(
                        sub="user123",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                        permissions=["read", "write"],
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return "mock-token"

        return MockProvider()

    @pytest.mark.asyncio
    async def test_passes_with_valid_token(self, mock_provider) -> None:
        """Middleware should pass requests with valid tokens."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=mock_provider)

        @app.get("/protected")
        def protected():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/protected", headers={"Authorization": "Bearer valid-token"})

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self, mock_provider) -> None:
        """Middleware should reject requests without token."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=mock_provider)

        @app.get("/protected")
        def protected():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/protected")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_invalid_token(self, mock_provider) -> None:
        """Middleware should reject requests with invalid token."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=mock_provider)

        @app.get("/protected")
        def protected():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/protected", headers={"Authorization": "Bearer invalid-token"})

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_excludes_configured_paths(self, mock_provider) -> None:
        """Middleware should skip auth for excluded paths."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(
            AuthMiddleware, provider=mock_provider, exclude_paths=["/health", "/public/*"]
        )

        @app.get("/health")
        def health():
            return {"status": "healthy"}

        @app.get("/public/info")
        def public_info():
            return {"info": "public"}

        client = TestClient(app)

        # Health endpoint should be accessible without auth
        response = client.get("/health")
        assert response.status_code == 200

        # Public paths should be accessible
        response = client.get("/public/info")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_injects_user_into_request(self, mock_provider) -> None:
        """Middleware should inject user claims into request state."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=mock_provider)

        @app.get("/whoami")
        def whoami(request: Request):
            user = request.state.user
            return {"user_id": user.sub, "permissions": user.permissions}

        client = TestClient(app)
        response = client.get("/whoami", headers={"Authorization": "Bearer valid-token"})

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user123"
        assert data["permissions"] == ["read", "write"]

    @pytest.mark.asyncio
    async def test_works_with_any_provider(self) -> None:
        """Middleware should work with any AuthProvider implementation."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import AuthMiddleware, AuthProvider, TokenClaims

        class CustomProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token.startswith("custom-"):
                    return TokenClaims(
                        sub=token.replace("custom-", ""),
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return f"custom-{claims['sub']}"

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=CustomProvider())

        @app.get("/test")
        def test_route():
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/test", headers={"Authorization": "Bearer custom-user456"})

        assert response.status_code == 200


class TestHelperFunctions:
    """Tests for auth helper functions."""

    def test_get_current_user(self) -> None:
        """get_current_user should return user from request state."""
        from unittest.mock import MagicMock

        from pymodules.api.auth import TokenClaims, get_current_user

        # Create mock request with user in state
        request = MagicMock()
        request.state.user = TokenClaims(
            sub="user123",
            exp=datetime.now(UTC) + timedelta(hours=1),
            iat=datetime.now(UTC),
        )

        user = get_current_user(request)

        assert user is not None
        assert user.sub == "user123"

    def test_require_auth_decorator(self) -> None:
        """require_auth should enforce authentication."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import (
            AuthMiddleware,
            AuthProvider,
            TokenClaims,
            require_auth,
        )

        class MockProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token == "valid":
                    return TokenClaims(
                        sub="user",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return "token"

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=MockProvider(), exclude_paths=["/public"])

        @app.get("/public")
        def public():
            return {"public": True}

        @app.get("/private")
        @require_auth
        def private(request: Request):
            return {"private": True}

        client = TestClient(app)

        # Public should work
        assert client.get("/public").status_code == 200

        # Private without auth should fail
        response = client.get("/private")
        assert response.status_code == 401

        # Private with auth should work
        response = client.get("/private", headers={"Authorization": "Bearer valid"})
        assert response.status_code == 200

    def test_require_permissions_decorator(self) -> None:
        """require_permissions should check user permissions."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api.auth import (
            AuthMiddleware,
            AuthProvider,
            TokenClaims,
            require_permissions,
        )

        class MockProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token == "admin":
                    return TokenClaims(
                        sub="admin",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                        permissions=["admin", "read", "write"],
                    )
                elif token == "reader":
                    return TokenClaims(
                        sub="reader",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                        permissions=["read"],
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return "token"

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=MockProvider())

        @app.get("/admin-only")
        @require_permissions(["admin"])
        def admin_only(request: Request):
            return {"admin": True}

        client = TestClient(app, raise_server_exceptions=False)

        # Admin should access
        response = client.get("/admin-only", headers={"Authorization": "Bearer admin"})
        assert response.status_code == 200

        # Reader should be forbidden
        response = client.get("/admin-only", headers={"Authorization": "Bearer reader"})
        assert response.status_code == 403
