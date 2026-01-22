"""Base auth abstractions.

Provides AuthProvider ABC and TokenClaims dataclass for pluggable authentication.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class TokenClaims:
    """Represents claims extracted from an authentication token.

    Attributes:
        sub: Subject (user ID)
        exp: Expiration time
        iat: Issued at time
        permissions: List of permission strings
        extra: Additional arbitrary claims
    """

    sub: str
    exp: datetime
    iat: datetime
    permissions: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if the token is expired."""
        return datetime.now(UTC) > self.exp


class AuthProvider(ABC):
    """Abstract base class for authentication providers.

    Implement this class to create custom authentication mechanisms
    (JWT, API keys, OAuth, etc.).

    Example:
        class MyAuthProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                # Validate and decode token
                ...

            async def create_token(self, claims: dict) -> str:
                # Create and encode token
                ...
    """

    @abstractmethod
    async def validate_token(self, token: str) -> TokenClaims | None:
        """Validate a token and return its claims.

        Args:
            token: The token string to validate

        Returns:
            TokenClaims if valid, None if invalid or expired
        """
        ...

    @abstractmethod
    async def create_token(self, claims: dict[str, Any]) -> str:
        """Create a new token from claims.

        Args:
            claims: Dictionary of claims to encode in the token

        Returns:
            Encoded token string
        """
        ...
