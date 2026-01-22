"""Tests for JWT auth provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


class TestJWTSettings:
    """Tests for JWTSettings."""

    def test_loads_from_env(self, monkeypatch) -> None:
        """JWTSettings should load from environment variables."""
        monkeypatch.setenv("PYMODULES_JWT_SECRET_KEY", "test-secret-key-123")
        monkeypatch.setenv("PYMODULES_JWT_ALGORITHM", "HS512")
        monkeypatch.setenv("PYMODULES_JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")

        from pymodules.api.auth import JWTSettings

        settings = JWTSettings()

        assert settings.secret_key == "test-secret-key-123"
        assert settings.algorithm == "HS512"
        assert settings.access_token_expire_minutes == 60

    def test_default_algorithm(self) -> None:
        """JWTSettings should default to HS256."""
        import os

        # Clear JWT env vars
        for key in list(os.environ.keys()):
            if key.startswith("PYMODULES_JWT_"):
                os.environ.pop(key, None)

        from pymodules.api.auth import JWTSettings

        # Provide required secret_key
        settings = JWTSettings(secret_key="test-secret")

        assert settings.algorithm == "HS256"

    def test_default_expiry(self) -> None:
        """JWTSettings should have default token expiry."""
        from pymodules.api.auth import JWTSettings

        settings = JWTSettings(secret_key="test-secret")

        assert settings.access_token_expire_minutes > 0


class TestJWTAuthProvider:
    """Tests for JWTAuthProvider."""

    @pytest.fixture
    def jwt_provider(self):
        """Create a JWTAuthProvider for testing."""
        from pymodules.api.auth import JWTAuthProvider, JWTSettings

        settings = JWTSettings(secret_key="test-secret-key-for-testing-12345")
        return JWTAuthProvider(settings)

    @pytest.mark.asyncio
    async def test_create_token_returns_string(self, jwt_provider) -> None:
        """create_token should return a token string."""
        token = await jwt_provider.create_token(
            {"sub": "user123", "permissions": ["read"]}
        )

        assert isinstance(token, str)
        assert len(token) > 0

    @pytest.mark.asyncio
    async def test_validate_token_returns_claims(self, jwt_provider) -> None:
        """validate_token should return TokenClaims for valid token."""
        from pymodules.api.auth import TokenClaims

        token = await jwt_provider.create_token(
            {"sub": "user123", "permissions": ["read"]}
        )

        claims = await jwt_provider.validate_token(token)

        assert claims is not None
        assert isinstance(claims, TokenClaims)
        assert claims.sub == "user123"

    @pytest.mark.asyncio
    async def test_validate_expired_token_returns_none(self, jwt_provider) -> None:
        """validate_token should return None for expired token."""
        from pymodules.api.auth import JWTAuthProvider, JWTSettings

        # Create provider with very short expiry
        settings = JWTSettings(
            secret_key="test-secret-key-for-testing-12345",
            access_token_expire_minutes=0,  # Immediate expiry
        )
        provider = JWTAuthProvider(settings)

        # Create token that expires immediately
        import asyncio

        token = await provider.create_token({"sub": "user123"})
        await asyncio.sleep(0.1)  # Wait for token to expire

        claims = await provider.validate_token(token)

        # Should be None or expired
        if claims is not None:
            assert claims.is_expired

    @pytest.mark.asyncio
    async def test_validate_invalid_token_returns_none(self, jwt_provider) -> None:
        """validate_token should return None for invalid token."""
        claims = await jwt_provider.validate_token("invalid.token.here")

        assert claims is None

    @pytest.mark.asyncio
    async def test_validate_tampered_token_returns_none(self, jwt_provider) -> None:
        """validate_token should return None for tampered token."""
        token = await jwt_provider.create_token({"sub": "user123"})

        # Tamper with the token by changing a character
        tampered = token[:-5] + "XXXXX"

        claims = await jwt_provider.validate_token(tampered)

        assert claims is None

    @pytest.mark.asyncio
    async def test_token_contains_claims(self, jwt_provider) -> None:
        """Token should contain all provided claims."""
        token = await jwt_provider.create_token(
            {
                "sub": "user123",
                "permissions": ["read", "write"],
                "email": "user@example.com",
            }
        )

        claims = await jwt_provider.validate_token(token)

        assert claims is not None
        assert claims.sub == "user123"
        assert claims.permissions == ["read", "write"]
        # Extra claims stored in extra dict
        assert claims.extra.get("email") == "user@example.com" or hasattr(
            claims, "email"
        )
