"""Full-stack tracer-bullet demo (issue #9 / ADR-0009).

This is the end-to-end runnable demonstration of ``pymodules.contrib.fullstack``:
one backend ``Module`` that handles a ``PostMessage`` Command, publishes a
``MessagePosted`` Event with a **tenant-scoped Outbound policy**, and a vanilla
HTML page that opens an SSE connection and renders incoming messages live.

What this shows
---------------

- Commands -> Module dispatch (``POST /messages``)
- In-process EventBus fan-out (``MessageModule`` publishes ``MessagePosted``)
- Deny-by-default Outbound policy with tenant filtering
  (``@outbound_policy(MessagePosted)`` returns ``event.tenant_id == client.tenant_id``)
- SSE push channel (``/__pymodules__/events?subscribe=MessagePosted``)
- Manifest endpoint (``/__pymodules__/manifest`` - OpenAPI + AsyncAPI)
- Cookie auth shim (HttpOnly access cookie, refresh router)
- Cross-tenant isolation: two browsers on different tenants never see each
  other's messages

Run
---

From the repo root::

    pip install -e ".[fullstack]"
    uvicorn examples.fullstack.app:app --reload --port 8000

Then open http://localhost:8000/ in two browser tabs and follow the README.

Stylistic note
--------------

This file mirrors ``examples/api_example.py`` (factory pattern, Command
dataclasses up top, Module in the middle, ``create_app()`` at the bottom)
and borrows the publish-via-host idiom from ``examples/eventbus_example.py``
(the Module holds a ``ModuleHost`` reference so its handler can call
``self._host.publish(...)``). Nothing here introduces a new framework
primitive — every import is a public API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Event,
    Module,
    ModuleHost,
    handles,
    module,
)
from pymodules.contrib.api import (
    ModuleRouter,
    api_endpoint,
    register_error_handlers,
)
from pymodules.contrib.api.auth import JWTAuthProvider, JWTSettings
from pymodules.contrib.fullstack import (
    DEFAULT_ACCESS_COOKIE,
    DEFAULT_REFRESH_COOKIE,
    ClientContext,
    attach_manifest_cache_invalidator,
    build_manifest_router,
    build_refresh_router,
    build_sse_router,
    make_cookie_auth_dependency,
    outbound_policy,
    register_with_host,
)

# =============================================================================
# Event + Command definitions
# =============================================================================


@dataclass
class MessagePosted(Event):
    """Broadcast when a message is posted in a tenant's room.

    The Outbound policy on ``MessageModule`` gates SSE delivery on
    ``event.tenant_id == client.tenant_id`` — this Event reaches only the
    posting tenant's connected browser clients.
    """

    tenant_id: str = ""
    body: str = ""
    posted_by: str = ""
    posted_at: str = ""
    name: str = "messages.message_posted"


@dataclass
class PostMessageInput(CommandRequest):
    """Request payload for posting a message.

    ``tenant_id`` and ``posted_by`` are accepted directly in v1 for demo
    simplicity. A production wiring would derive both from the
    ``ClientContext`` of the cookie-authenticated caller rather than from
    the request body — see the README's "production gaps" section.
    """

    tenant_id: str = ""
    body: str = ""
    posted_by: str = ""


@dataclass
class PostMessageOutput(CommandResponse):
    """Confirmation that the message was accepted and the Event published."""

    ok: bool = True
    tenant_id: str = ""
    posted_by: str = ""
    posted_at: str = ""


@api_endpoint(method="POST", path="/messages", tags=["Messages"])
class PostMessage(Command[PostMessageInput, PostMessageOutput]):
    """Command to post a message and fan out a ``MessagePosted`` Event."""

    name = "messages.post"


# =============================================================================
# Module
# =============================================================================


@module(name="messages", description="Posts messages and broadcasts them per-tenant")
class MessageModule(Module):
    """Handles ``PostMessage`` and publishes ``MessagePosted`` via the host.

    The Module holds a back-reference to its ``ModuleHost`` so its handler
    can call ``self._host.publish(...)``. This is the same publish-via-host
    idiom used in ``examples/eventbus_example.py``. The ``@outbound_policy``
    decorator wires the tenant-scope filter into
    ``host.outbound_policies`` at registration time.
    """

    # Declares which Events this Module publishes. Consumed by the manifest
    # endpoint's AsyncAPI emitter and by the SSE endpoint's subscribe-name
    # resolver. An Event not on this tuple cannot be subscribed to.
    published_events = (MessagePosted,)

    def __init__(self, host: ModuleHost) -> None:
        super().__init__()
        # See ``examples/eventbus_example.py``: Modules don't get a host
        # reference automatically; the example wires one in. Production code
        # would typically inject ``host.event_bus`` (a smaller surface) but
        # for the demo holding the whole host keeps the example obvious.
        self._host = host

    @handles(PostMessage)
    def post_message(self, command: PostMessage) -> PostMessageOutput:
        req = command.request
        assert req is not None  # narrow Optional for mypy

        posted_at = datetime.now(timezone.utc).isoformat()

        # Publish in-process. The SSE router subscribes per-connection and
        # applies the Outbound policy below before pushing to each client.
        self._host.publish(
            MessagePosted(
                tenant_id=req.tenant_id,
                body=req.body,
                posted_by=req.posted_by,
                posted_at=posted_at,
            )
        )

        return PostMessageOutput(
            ok=True,
            tenant_id=req.tenant_id,
            posted_by=req.posted_by,
            posted_at=posted_at,
        )

    @outbound_policy(MessagePosted)
    def gate_by_tenant(
        self, event: MessagePosted, client: ClientContext
    ) -> bool:
        """Tenant-scoped outbound filter.

        Returns ``True`` only when the Event's ``tenant_id`` matches the
        connected client's ``ClientContext.tenant_id``. Cross-tenant leakage
        is impossible by construction — without this method the SSE
        endpoint would refuse the subscription up-front
        (deny-by-default per ADR-0009).
        """
        return event.tenant_id == client.tenant_id


# =============================================================================
# Demo-mode login (NOT for production)
# =============================================================================

# Demo-only secret. A real app would load this from env or a secrets
# manager; we hard-code a value here so the example runs without setup.
# Length must be >= 32 chars for python-jose to be happy with HS256.
_DEMO_SECRET = "demo-only-secret-do-not-use-in-production-please-1234567890"


def _make_jwt_provider() -> JWTAuthProvider:
    """Build the JWT provider used by both the cookie shim and the dev login.

    A single provider instance is shared so the cookies the dev login mints
    are validated by the same secret/algorithm the SSE auth check uses.
    """
    return JWTAuthProvider(
        JWTSettings(
            secret_key=_DEMO_SECRET,
            algorithm="HS256",
            # Short enough that you'll see the refresh router in action if
            # you sit on the page; long enough that demo curl-ing doesn't
            # require constant re-login.
            access_token_expire_minutes=30,
        )
    )


# =============================================================================
# Application factory
# =============================================================================


# Path to the static index.html shipped alongside this file. Resolved once
# at import time so reloads don't recompute it.
_HERE = Path(__file__).resolve().parent
_INDEX_HTML = _HERE / "index.html"


def create_app() -> FastAPI:
    """Wire the ``ModuleHost``, FastAPI app, and fullstack routers together."""

    # ----- ModuleHost ------------------------------------------------------
    host = ModuleHost()

    # The Module needs the host reference for ``self._host.publish(...)``;
    # see ``examples/eventbus_example.py`` for the canonical pattern.
    host.register(MessageModule(host=host))

    # ----- FastAPI app -----------------------------------------------------
    app = FastAPI(
        title="PyModules Full-Stack Demo",
        description=(
            "End-to-end demo of pymodules.contrib.fullstack: Commands, "
            "Events, SSE push channel, and tenant-scoped Outbound policy."
        ),
        version="0.1.0",
    )
    register_error_handlers(app)

    # ----- Command -> HTTP endpoint mapping --------------------------------
    # ``PostMessage`` was decorated with ``@api_endpoint(POST /messages)``
    # above; ``ModuleRouter`` consumes that mapping.
    cmd_router = ModuleRouter(host)
    cmd_router.register_command(PostMessage)
    cmd_router.mount(app)

    # ----- Auth wiring -----------------------------------------------------
    # One JWT provider feeds both the cookie auth dependency (SSE +
    # manifest) and the refresh router. The dev login below also reuses it.
    jwt_provider = _make_jwt_provider()
    cookie_auth = make_cookie_auth_dependency(jwt_provider)

    # secure=False: the demo runs over plain HTTP. In production set
    # secure=True (or omit — that's the default).
    app.include_router(
        build_refresh_router(jwt_provider, secure=False),
    )

    # ----- Manifest endpoint ----------------------------------------------
    manifest_router = build_manifest_router(
        host,
        fastapi_app=app,
        cookie_auth_dependency=cookie_auth,
    )
    app.include_router(manifest_router)
    # Invalidate the manifest cache when a Module (re-)registers. Harmless
    # here (we register once at startup) but matches production wiring.
    attach_manifest_cache_invalidator(host, manifest_router.invalidate)

    # ----- SSE push endpoint ----------------------------------------------
    sse_router = build_sse_router(host, cookie_auth_dependency=cookie_auth)
    app.include_router(sse_router)
    # Drain in-flight SSE connections on host shutdown.
    register_with_host(host, sse_router)

    # ----- Dev-mode login (NOT FOR PRODUCTION) ----------------------------
    # The PRD explicitly scopes the cookie-setting login endpoint out of
    # framework code as "application-specific". This demo ships a trivial
    # shortcut so a fresh clone can run the demo without standing up an
    # identity provider. *Do not copy this into a real app.*
    @app.post("/__demo__/login", tags=["demo"])
    async def demo_login(
        response: Response,
        user_id: str = "alice",
        tenant_id: str = "tenant-a",
    ) -> dict[str, str]:
        """Mint an access cookie for a hardcoded (user, tenant) pair.

        DEV ONLY. Real applications integrate a proper identity provider
        and never trust client-supplied user/tenant fields. See the PRD's
        "out of scope" section.
        """
        access_token = await jwt_provider.create_token(
            {"sub": user_id, "tenant_id": tenant_id}
        )
        refresh_token = await jwt_provider.create_token(
            {"sub": user_id, "tenant_id": tenant_id}
        )
        # Mirrors the attributes ``build_refresh_router`` sets on rotation.
        response.set_cookie(
            key=DEFAULT_ACCESS_COOKIE,
            value=access_token,
            httponly=True,
            samesite="strict",
            secure=False,  # demo runs on plain HTTP
            path="/",
        )
        response.set_cookie(
            key=DEFAULT_REFRESH_COOKIE,
            value=refresh_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/__pymodules__/auth/refresh",
        )
        return {
            "logged_in_as": user_id,
            "tenant_id": tenant_id,
            "warning": "demo login - not for production",
        }

    @app.post("/__demo__/logout", tags=["demo"])
    async def demo_logout(response: Response) -> dict[str, bool]:
        """Clear the demo cookies."""
        response.delete_cookie(DEFAULT_ACCESS_COOKIE, path="/")
        response.delete_cookie(
            DEFAULT_REFRESH_COOKIE, path="/__pymodules__/auth/refresh"
        )
        return {"logged_out": True}

    # ----- Static index.html ----------------------------------------------
    # Serve the demo HTML at "/". Using a single ``FileResponse`` route
    # rather than ``StaticFiles`` keeps the example dependency-free (no
    # ``aiofiles`` requirement) and ergonomic.
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_INDEX_HTML, media_type="text/html")

    @app.get("/health", tags=["demo"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Module-level ``app`` for ``uvicorn examples.fullstack.app:app``.
app = create_app()


# =============================================================================
# CLI entry-point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print(
        """
=============================================================================
PyModules Full-Stack Demo
=============================================================================

Starting at http://localhost:8000

Open http://localhost:8000/ in two browser tabs. Log each tab in to a
different tenant ("tenant-a" / "tenant-b") via the form. Then post a
message from one tab and watch ONLY that tenant's tab receive the SSE
event — cross-tenant isolation enforced by the Outbound policy.

See examples/fullstack/README.md for curl-based equivalents.
=============================================================================
"""
    )
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
