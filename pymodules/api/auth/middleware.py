"""Authentication middleware.

Provides AuthMiddleware for FastAPI/Starlette applications.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .base import AuthProvider, TokenClaims

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication middleware using pluggable AuthProvider.

    Validates Bearer tokens in the Authorization header and injects
    user claims into request.state.user.

    Example:
        from fastapi import FastAPI
        from pymodules.api.auth import AuthMiddleware, JWTAuthProvider

        app = FastAPI()
        provider = JWTAuthProvider(settings)
        app.add_middleware(
            AuthMiddleware,
            provider=provider,
            exclude_paths=["/health", "/public/*"]
        )
    """

    def __init__(
        self,
        app: Any,
        provider: AuthProvider,
        exclude_paths: list[str] | None = None,
    ):
        """Initialize the auth middleware.

        Args:
            app: ASGI application
            provider: AuthProvider implementation for token validation
            exclude_paths: List of paths to exclude from auth (supports wildcards)
        """
        super().__init__(app)
        self.provider = provider
        self.exclude_paths = exclude_paths or []

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        """Process the request through auth middleware."""
        # Check if path is excluded
        path = request.url.path
        if self._is_excluded(path):
            return await call_next(request)

        # Extract token from Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing authorization header"},
            )

        # Parse Bearer token
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid authorization header format"},
            )

        token = parts[1]

        # Validate token
        claims = await self.provider.validate_token(token)
        if claims is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        # Check if token is expired
        if claims.is_expired:
            return JSONResponse(
                status_code=401,
                content={"detail": "Token has expired"},
            )

        # Inject user into request state
        request.state.user = claims

        return await call_next(request)

    def _is_excluded(self, path: str) -> bool:
        """Check if a path is excluded from authentication."""
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.exclude_paths)


def get_current_user(request: Any) -> TokenClaims | None:
    """Get the current user from request state.

    Args:
        request: FastAPI/Starlette request object

    Returns:
        TokenClaims if authenticated, None otherwise
    """
    return getattr(request.state, "user", None)


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to require authentication on an endpoint.

    Use this for routes that are excluded from middleware but still
    need authentication.

    Example:
        @app.get("/optional-auth")
        @require_auth
        def optional_auth_route(request: Request):
            user = request.state.user
            return {"user": user.sub}
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Find request in args or kwargs
        request = None
        for arg in args:
            if hasattr(arg, "state"):
                request = arg
                break
        if request is None:
            request = kwargs.get("request")

        if request is None:
            return JSONResponse(
                status_code=500,
                content={"detail": "Request not found in handler arguments"},
            )

        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )

        return await func(*args, **kwargs) if _is_async(func) else func(*args, **kwargs)

    return wrapper


def require_permissions(
    permissions: list[str], require_all: bool = False
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to require specific permissions on an endpoint.

    Args:
        permissions: List of required permissions
        require_all: If True, user must have ALL permissions.
                    If False (default), user needs ANY of the permissions.

    Example:
        @app.get("/admin")
        @require_permissions(["admin"])
        def admin_route(request: Request):
            return {"admin": True}

        @app.get("/editor")
        @require_permissions(["edit", "admin"], require_all=False)
        def editor_route(request: Request):
            return {"editor": True}
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Find request in args or kwargs
            request = None
            for arg in args:
                if hasattr(arg, "state"):
                    request = arg
                    break
            if request is None:
                request = kwargs.get("request")

            if request is None:
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Request not found in handler arguments"},
                )

            user = getattr(request.state, "user", None)
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )

            user_perms = set(user.permissions)
            required_perms = set(permissions)

            if require_all:
                has_permission = required_perms.issubset(user_perms)
            else:
                has_permission = bool(required_perms & user_perms)

            if not has_permission:
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Insufficient permissions",
                        "required": permissions,
                    },
                )

            return await func(*args, **kwargs) if _is_async(func) else func(*args, **kwargs)

        return wrapper

    return decorator


def _is_async(func: Callable[..., Any]) -> bool:
    """Check if a function is async."""
    import asyncio

    return asyncio.iscoroutinefunction(func)
