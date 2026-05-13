"""JWT authentication provider.

Provides JWTAuthProvider for JWT-based authentication.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from .base import AuthProvider, TokenClaims


class JWTSettings(BaseSettings):
    """Settings for JWT authentication.

    Environment Variables:
        PYMODULES_JWT_SECRET_KEY: Secret key for signing tokens (required)
        PYMODULES_JWT_ALGORITHM: Algorithm to use (default: HS256)
        PYMODULES_JWT_ACCESS_TOKEN_EXPIRE_MINUTES: Token expiry in minutes (default: 30)
    """

    model_config = SettingsConfigDict(
        env_prefix="PYMODULES_JWT_",
        extra="ignore",
    )

    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30


class JWTAuthProvider(AuthProvider):
    """JWT-based authentication provider.

    Uses python-jose for JWT encoding/decoding.

    Example:
        settings = JWTSettings(secret_key="your-secret-key")
        provider = JWTAuthProvider(settings)

        # Create a token
        token = await provider.create_token({"sub": "user123"})

        # Validate a token
        claims = await provider.validate_token(token)
    """

    def __init__(self, settings: JWTSettings | None = None):
        """Initialize the JWT provider.

        Args:
            settings: JWT settings (loads from env if not provided)
        """
        self.settings = settings or JWTSettings()

    async def validate_token(self, token: str) -> TokenClaims | None:
        """Validate a JWT token and return its claims.

        Args:
            token: JWT token string

        Returns:
            TokenClaims if valid, None if invalid or expired
        """
        try:
            from jose import JWTError, jwt
        except ImportError as e:
            raise ImportError(
                "python-jose is required for JWT auth. "
                "Install it with: pip install 'pymodules[jwt]'"
            ) from e

        try:
            payload = jwt.decode(
                token,
                self.settings.secret_key,
                algorithms=[self.settings.algorithm],
            )

            sub = payload.get("sub")
            if sub is None:
                return None

            # Parse expiration and issued at times
            exp = payload.get("exp")
            iat = payload.get("iat")

            if exp is None or iat is None:
                return None

            exp_dt = datetime.fromtimestamp(exp, tz=UTC)
            iat_dt = datetime.fromtimestamp(iat, tz=UTC)

            # Extract permissions if present
            permissions = payload.get("permissions", [])

            # Put remaining claims in extra
            standard_claims = {"sub", "exp", "iat", "permissions"}
            extra = {k: v for k, v in payload.items() if k not in standard_claims}

            return TokenClaims(
                sub=sub,
                exp=exp_dt,
                iat=iat_dt,
                permissions=permissions,
                extra=extra,
            )

        except JWTError:
            return None
        except Exception:
            return None

    async def create_token(self, claims: dict[str, Any]) -> str:
        """Create a JWT token from claims.

        Args:
            claims: Claims to encode (must include 'sub')

        Returns:
            Encoded JWT token string
        """
        try:
            from jose import jwt
        except ImportError as e:
            raise ImportError(
                "python-jose is required for JWT auth. "
                "Install it with: pip install 'pymodules[jwt]'"
            ) from e

        # Calculate expiration
        expire = datetime.now(UTC) + timedelta(minutes=self.settings.access_token_expire_minutes)

        # Build token payload
        to_encode = claims.copy()
        to_encode["exp"] = expire
        to_encode["iat"] = datetime.now(UTC)

        encoded_jwt = jwt.encode(
            to_encode,
            self.settings.secret_key,
            algorithm=self.settings.algorithm,
        )

        return encoded_jwt
