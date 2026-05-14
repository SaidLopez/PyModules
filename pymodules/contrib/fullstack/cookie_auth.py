"""Cookie auth shim for browser clients.

This module provides the **read-and-refresh** half of the cookie auth story
locked in ADR-0009. It deliberately does not own the login endpoint that
*sets* the cookies — that is application-specific and out of scope for
framework code.

What it ships
-------------

- :func:`make_cookie_auth_dependency` — a factory returning a FastAPI
  dependency that:

  1. Reads a configured cookie (default ``pymodules_access``).
  2. Validates it through the existing
     :class:`pymodules.contrib.api.auth.JWTAuthProvider` (or any
     ``AuthProvider`` subclass — same contract as ``AuthMiddleware``).
  3. On success, constructs and returns a :class:`ClientContext` mapping
     ``sub`` → ``user_id``, the ``tenant_id`` claim → ``tenant_id``, and
     all decoded claims → ``claims``.
  4. On missing or expired cookie, raises ``HTTPException(401)`` with
     ``WWW-Authenticate: Cookie realm="pymodules"`` (per the AC and the
     ADR-0009 contract).

- :func:`build_refresh_router` — a factory returning a FastAPI ``APIRouter``
  that mounts the ``/__pymodules__/auth/refresh`` endpoint. The endpoint:

  1. Reads the configured refresh cookie (default ``pymodules_refresh``).
  2. Validates it via the same ``AuthProvider`` (same JWT family — no new
     claim format is introduced, per ADR-0009).
  3. Rotates **both** cookies via ``provider.create_token({"sub": ...,
     "tenant_id": ...})`` and sets them with ``HttpOnly``, ``SameSite=Strict``,
     ``Secure`` (in production config; toggleable for tests).

Both factories accept the cookie names as parameters; defaults match the PRD.
A ``secure`` flag on :func:`build_refresh_router` toggles the ``Secure``
cookie attribute (kept ``True`` by default; flipped ``False`` in tests
because ``TestClient`` does not run over TLS).

Bearer-token regression
-----------------------

This shim does **not** touch ``pymodules.contrib.api.auth.AuthMiddleware``.
Routes that opt into cookie auth do so via ``Depends(cookie_auth_required)``;
all other routes continue to authenticate via ``AuthMiddleware`` exactly as
before. The two paths coexist; the cookie shim is opt-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Cookie, HTTPException, Response, status

from .client_context import ClientContext

if TYPE_CHECKING:
    from pymodules.contrib.api.auth import AuthProvider, TokenClaims


# Default cookie names. The PRD pins these (``pymodules_access`` /
# ``pymodules_refresh``); factory parameters allow overrides per host.
DEFAULT_ACCESS_COOKIE = "pymodules_access"
DEFAULT_REFRESH_COOKIE = "pymodules_refresh"

# The PRD's exact 401 challenge header. Pinned as a constant so the test
# assertion and the runtime value can never drift.
_WWW_AUTHENTICATE_COOKIE = 'Cookie realm="pymodules"'


def _claims_to_client_context(claims: TokenClaims) -> ClientContext:
    """Build a :class:`ClientContext` from a validated :class:`TokenClaims`.

    Maps:

    - ``sub``       → ``user_id``
    - ``tenant_id`` (from ``claims.extra``) → ``tenant_id`` (None if absent)
    - The full reconstructed claims dict → ``claims``

    The full ``claims`` dict is rebuilt from ``TokenClaims`` rather than the
    raw JWT so policies see the same shape regardless of which
    ``AuthProvider`` validated the token.
    """
    tenant_id_raw = claims.extra.get("tenant_id")
    tenant_id = tenant_id_raw if isinstance(tenant_id_raw, str) else None

    flat_claims: dict[str, Any] = {
        "sub": claims.sub,
        "exp": int(claims.exp.timestamp()),
        "iat": int(claims.iat.timestamp()),
        "permissions": list(claims.permissions),
    }
    flat_claims.update(claims.extra)

    return ClientContext(
        user_id=claims.sub,
        tenant_id=tenant_id,
        claims=flat_claims,
    )


def make_cookie_auth_dependency(
    provider: AuthProvider,
    *,
    access_cookie_name: str = DEFAULT_ACCESS_COOKIE,
):
    """Factory returning a FastAPI dependency that authenticates via cookie.

    Args:
        provider: An ``AuthProvider`` (typically ``JWTAuthProvider``). Reused
            from ``pymodules.contrib.api.auth`` — no new JWT plumbing.
        access_cookie_name: Name of the access cookie to read. Defaults to
            ``"pymodules_access"`` per the PRD.

    Returns:
        An async callable usable as ``Depends(...)`` on a FastAPI route. On
        success it returns a :class:`ClientContext`; on failure it raises
        ``HTTPException(401)`` with the ``WWW-Authenticate: Cookie
        realm="pymodules"`` challenge header.

    Example:

        provider = JWTAuthProvider(settings)
        cookie_auth = make_cookie_auth_dependency(provider)

        @app.get("/me")
        async def me(client: ClientContext = Depends(cookie_auth)) -> dict:
            return {"user_id": client.user_id, "tenant_id": client.tenant_id}
    """

    async def cookie_auth_required(
        # Use ``Cookie(..., alias=...)`` so FastAPI extracts by the configured
        # name. Defaulting to ``None`` lets us produce a structured 401 rather
        # than FastAPI's default 422 when the cookie is absent.
        access_token: str | None = Cookie(default=None, alias=access_cookie_name),
    ) -> ClientContext:
        if access_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing access cookie",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE_COOKIE},
            )

        claims = await provider.validate_token(access_token)
        if claims is None or claims.is_expired:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access cookie",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE_COOKIE},
            )

        return _claims_to_client_context(claims)

    return cookie_auth_required


def _set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_cookie_name: str,
    refresh_cookie_name: str,
    secure: bool,
    refresh_path: str,
) -> None:
    """Write the rotated access + refresh cookies on the response.

    Cookie attributes are pinned per ADR-0009:

    - ``HttpOnly=True``        — JS can never read these cookies.
    - ``SameSite="Strict"``    — CSRF-safe for the dispatch surface.
    - ``Secure=<secure>``      — True in production; False under
      ``TestClient`` (which is plain HTTP).
    - ``path="/"`` for the access cookie (sent to every request).
    - ``path=<refresh_path>`` for the refresh cookie (scoped to the refresh
      endpoint so it never leaks to other handlers).
    """
    response.set_cookie(
        key=access_cookie_name,
        value=access_token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        key=refresh_cookie_name,
        value=refresh_token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path=refresh_path,
    )


def build_refresh_router(
    provider: AuthProvider,
    *,
    access_cookie_name: str = DEFAULT_ACCESS_COOKIE,
    refresh_cookie_name: str = DEFAULT_REFRESH_COOKIE,
    secure: bool = True,
    path: str = "/__pymodules__/auth/refresh",
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` exposing the refresh endpoint.

    Args:
        provider: The same ``AuthProvider`` that issued the access and
            refresh tokens. Validation and re-issue both flow through it,
            so no new claim format is introduced.
        access_cookie_name: Name of the access cookie to rotate. Defaults
            to ``"pymodules_access"``.
        refresh_cookie_name: Name of the refresh cookie to read and
            rotate. Defaults to ``"pymodules_refresh"``.
        secure: Value for the ``Secure`` cookie attribute. ``True`` in
            production (where the host is behind TLS); pass ``False`` for
            ``TestClient`` integration tests, which use plain HTTP.
        path: Path the refresh endpoint is mounted at. The refresh cookie
            is scoped to this path so other handlers never see it.

    Returns:
        An ``APIRouter`` with a single ``POST`` route at ``path``. Mount it
        on the application with ``app.include_router(router)``.

    Endpoint behaviour
    ------------------

    - On valid refresh cookie: rotates **both** cookies (a new access token
      and a new refresh token via ``provider.create_token({"sub": ...,
      "tenant_id": ...})``) and returns ``200`` with body
      ``{"refreshed": True}``.
    - On missing or invalid refresh cookie: returns ``401`` with
      ``WWW-Authenticate: Cookie realm="pymodules"``.
    """
    router = APIRouter()

    @router.post(path)
    async def refresh(
        response: Response,
        refresh_token: str | None = Cookie(default=None, alias=refresh_cookie_name),
    ) -> dict[str, bool]:
        if refresh_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing refresh cookie",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE_COOKIE},
            )

        claims = await provider.validate_token(refresh_token)
        if claims is None or claims.is_expired:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh cookie",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE_COOKIE},
            )

        # Re-issue both tokens using the same provider. Preserve the user's
        # core identity (``sub``, ``tenant_id``, ``permissions``) and any
        # extra claims that were on the refresh token — this matches the
        # "no new claim format" constraint in ADR-0009.
        rotate_claims: dict[str, Any] = {
            "sub": claims.sub,
            "permissions": list(claims.permissions),
        }
        rotate_claims.update(claims.extra)

        new_access = await provider.create_token(rotate_claims)
        new_refresh = await provider.create_token(rotate_claims)

        _set_auth_cookies(
            response,
            access_token=new_access,
            refresh_token=new_refresh,
            access_cookie_name=access_cookie_name,
            refresh_cookie_name=refresh_cookie_name,
            secure=secure,
            refresh_path=path,
        )

        return {"refreshed": True}

    return router


__all__ = [
    "ClientContext",
    "DEFAULT_ACCESS_COOKIE",
    "DEFAULT_REFRESH_COOKIE",
    "build_refresh_router",
    "make_cookie_auth_dependency",
]
