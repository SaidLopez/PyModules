"""Tests for auth base abstractions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


class TestTokenClaims:
    """Tests for TokenClaims dataclass."""

    def test_required_fields(self) -> None:
        """TokenClaims should require sub, exp, iat fields."""
        from pymodules.api.auth import TokenClaims

        now = datetime.now(UTC)
        claims = TokenClaims(
            sub="user123",
            exp=now + timedelta(hours=1),
            iat=now,
        )

        assert claims.sub == "user123"
        assert claims.exp > now
        assert claims.iat == now

    def test_optional_permissions(self) -> None:
        """TokenClaims should support optional permissions."""
        from pymodules.api.auth import TokenClaims

        now = datetime.now(UTC)
        claims = TokenClaims(
            sub="user123",
            exp=now + timedelta(hours=1),
            iat=now,
            permissions=["read", "write"],
        )

        assert claims.permissions == ["read", "write"]

    def test_optional_extra(self) -> None:
        """TokenClaims should support extra arbitrary data."""
        from pymodules.api.auth import TokenClaims

        now = datetime.now(UTC)
        claims = TokenClaims(
            sub="user123",
            exp=now + timedelta(hours=1),
            iat=now,
            extra={"role": "admin", "tenant_id": "t1"},
        )

        assert claims.extra["role"] == "admin"
        assert claims.extra["tenant_id"] == "t1"

    def test_is_expired(self) -> None:
        """TokenClaims.is_expired should return True for expired tokens."""
        from pymodules.api.auth import TokenClaims

        now = datetime.now(UTC)

        # Expired token
        expired_claims = TokenClaims(
            sub="user123",
            exp=now - timedelta(hours=1),
            iat=now - timedelta(hours=2),
        )
        assert expired_claims.is_expired is True

        # Valid token
        valid_claims = TokenClaims(
            sub="user123",
            exp=now + timedelta(hours=1),
            iat=now,
        )
        assert valid_claims.is_expired is False


class TestAuthProvider:
    """Tests for AuthProvider ABC."""

    def test_is_abstract(self) -> None:
        """AuthProvider should be abstract."""
        from pymodules.api.auth import AuthProvider

        with pytest.raises(TypeError, match="abstract"):
            AuthProvider()

    def test_requires_validate_token(self) -> None:
        """AuthProvider should require validate_token method."""
        from pymodules.api.auth import AuthProvider

        # Check that validate_token is abstract
        assert hasattr(AuthProvider, "validate_token")
        assert getattr(AuthProvider.validate_token, "__isabstractmethod__", False)

    def test_requires_create_token(self) -> None:
        """AuthProvider should require create_token method."""
        from pymodules.api.auth import AuthProvider

        assert hasattr(AuthProvider, "create_token")
        assert getattr(AuthProvider.create_token, "__isabstractmethod__", False)

    def test_concrete_implementation_works(self) -> None:
        """A concrete AuthProvider implementation should work."""
        from datetime import UTC, datetime, timedelta

        from pymodules.api.auth import AuthProvider, TokenClaims

        class MockAuthProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token == "valid":
                    return TokenClaims(
                        sub="user1",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return "mock_token"

        provider = MockAuthProvider()
        assert provider is not None
