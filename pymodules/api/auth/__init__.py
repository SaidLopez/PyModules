"""Pluggable authentication subsystem.

Provides:
- AuthProvider: Abstract base class for authentication providers
- TokenClaims: Dataclass representing decoded token claims
- JWTAuthProvider: JWT-based authentication provider
- JWTSettings: Configuration for JWT authentication
- AuthMiddleware: Starlette/FastAPI middleware for authentication
- Helper functions: get_current_user, require_auth, require_permissions
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import AuthProvider, TokenClaims
    from .jwt import JWTAuthProvider, JWTSettings
    from .middleware import (
        AuthMiddleware,
        get_current_user,
        require_auth,
        require_permissions,
    )

__all__ = [
    # Base abstractions
    "AuthProvider",
    "TokenClaims",
    # JWT provider
    "JWTAuthProvider",
    "JWTSettings",
    # Middleware
    "AuthMiddleware",
    "get_current_user",
    "require_auth",
    "require_permissions",
]


def __getattr__(name: str):
    """Lazy load auth components."""
    if name in ("AuthProvider", "TokenClaims"):
        from .base import AuthProvider, TokenClaims

        return {"AuthProvider": AuthProvider, "TokenClaims": TokenClaims}[name]

    if name in ("JWTAuthProvider", "JWTSettings"):
        from .jwt import JWTAuthProvider, JWTSettings

        return {"JWTAuthProvider": JWTAuthProvider, "JWTSettings": JWTSettings}[name]

    if name in ("AuthMiddleware", "get_current_user", "require_auth", "require_permissions"):
        from .middleware import (
            AuthMiddleware,
            get_current_user,
            require_auth,
            require_permissions,
        )

        return {
            "AuthMiddleware": AuthMiddleware,
            "get_current_user": get_current_user,
            "require_auth": require_auth,
            "require_permissions": require_permissions,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
