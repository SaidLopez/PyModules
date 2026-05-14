"""Tests for the SSE push endpoint (issue #7).

Covers every acceptance-criterion bullet from ``gh issue 7``:

- Always-true policy: published Event reaches the client in the correct
  SSE wire format.
- Tenant-match policy: only matching-tenant Events reach a given client;
  cross-tenant clients see nothing.
- No Outbound policy: 400 with ``no_outbound_policy`` body.
- Unknown Event name: 400 with ``unknown_event`` body.
- No auth cookie: 401 with ``WWW-Authenticate: Cookie``.
- Expired auth cookie: 401, same header.
- Multiple Event classes in one ``?subscribe=`` are all routed.
- A raising policy callable on one connection does not affect siblings.
- Client disconnect cleanly unsubscribes from the EventBus.

Harness notes
-------------

FastAPI's :class:`TestClient` and :class:`httpx.ASGITransport` both
**buffer the response body to completion** before returning a ``Response``
object. That is fatal for SSE: the stream never ends on its own, so the
test would deadlock waiting for ``more_body=False``. We therefore run
the FastAPI app behind a real :class:`uvicorn.Server` on a free local
port (started in a background thread, torn down per-test by a fixture)
and consume the SSE stream over a real socket via
:class:`httpx.AsyncClient.stream`. This mirrors how a browser's
``EventSource`` would talk to the endpoint and is the only way to
exercise the streaming path honestly.

Non-streaming assertions (auth failures and validation 400s) still use
``TestClient`` because their response bodies complete immediately.

``asyncio_mode = "auto"`` is set in ``pyproject.toml``, so ``async def``
test methods are picked up by pytest-asyncio without explicit marks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymodules import Event, Module, ModuleHost
from pymodules.contrib.fullstack import (
    ClientContext,
    MissingOutboundPolicy,
    UnknownEventSubscription,
    build_sse_router,
    make_cookie_auth_dependency,
    outbound_policy,
)


# ---------------------------------------------------------------------------
# Synthetic Events + Modules used across the test cases.
# ---------------------------------------------------------------------------


@dataclass
class MessagePosted(Event):
    tenant_id: str = ""
    body: str = ""
    name: str = "message.posted"


@dataclass
class OrderPlaced(Event):
    tenant_id: str = ""
    order_id: str = ""
    name: str = "order.placed"


@dataclass
class UnpoliciedEvent(Event):
    """Declared on a Module but with no ``@outbound_policy`` wired.

    Used to exercise the ``MissingOutboundPolicy`` branch.
    """

    payload: str = ""
    name: str = "unpolicied"


class AlwaysTrueModule(Module):
    """Publishes ``MessagePosted`` with a permissive policy (every client passes)."""

    published_events = (MessagePosted,)

    @outbound_policy(MessagePosted)
    def gate_message(
        self, event: MessagePosted, client: ClientContext
    ) -> bool:
        return True


class TenantMatchModule(Module):
    """Publishes ``MessagePosted`` with a tenant-scoped policy."""

    published_events = (MessagePosted,)

    @outbound_policy(MessagePosted)
    def gate_by_tenant(
        self, event: MessagePosted, client: ClientContext
    ) -> bool:
        return event.tenant_id == client.tenant_id


class TwoEventsModule(Module):
    """Publishes two Event classes with always-true policies."""

    published_events = (MessagePosted, OrderPlaced)

    @outbound_policy(MessagePosted)
    def gate_message(
        self, event: MessagePosted, client: ClientContext
    ) -> bool:
        return True

    @outbound_policy(OrderPlaced)
    def gate_order(
        self, event: OrderPlaced, client: ClientContext
    ) -> bool:
        return True


class UnpoliciedModule(Module):
    """Declares ``UnpoliciedEvent`` as published but registers no policy."""

    published_events = (UnpoliciedEvent,)


class RaisingPolicyModule(Module):
    """Outbound policy that raises for a specific client (per-connection isolation)."""

    published_events = (MessagePosted,)

    @outbound_policy(MessagePosted)
    def explode_for_bomb_user(
        self, event: MessagePosted, client: ClientContext
    ) -> bool:
        if client.user_id == "bomb-user":
            raise RuntimeError("policy boom")
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_provider():
    from pymodules.contrib.api.auth import JWTAuthProvider, JWTSettings

    settings = JWTSettings(
        secret_key="test-secret-key-for-sse-tests-1234567890",
        access_token_expire_minutes=30,
    )
    return JWTAuthProvider(settings)


@pytest.fixture
def issue_token_async(jwt_provider):
    """Coroutine helper that mints a JWT on the test loop."""

    async def _issue(claims: dict[str, Any]) -> str:
        return await jwt_provider.create_token(claims)

    return _issue


@pytest.fixture
def issue_token_sync(jwt_provider):
    """Sync helper for non-streaming tests."""

    def _issue(claims: dict[str, Any]) -> str:
        return asyncio.get_event_loop().run_until_complete(
            jwt_provider.create_token(claims)
        )

    return _issue


def _make_expired_jwt(provider, claims: dict[str, Any]) -> str:
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


def _build_app(host: ModuleHost, jwt_provider) -> FastAPI:
    cookie_auth = make_cookie_auth_dependency(jwt_provider)
    router = build_sse_router(host, cookie_auth_dependency=cookie_auth)
    app = FastAPI()
    app.include_router(router)
    return app


def _free_port() -> int:
    """Allocate an ephemeral port and immediately release it.

    Race-prone in theory, fine in practice for a single test process. We
    bind to ``127.0.0.1:0`` so the kernel picks an unused port, then
    close — uvicorn re-binds it microseconds later.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RunningServer:
    """A uvicorn server running in a background thread, bound to a free port.

    The streaming-response tests need a real socket because both
    ``TestClient`` and ``httpx.ASGITransport`` buffer the response body
    to completion (they never return until ``more_body=False``). The
    server's loop is the one that hosts the streaming generator, so the
    EventBus' ``call_soon_threadsafe`` correctly wakes the per-connection
    queue.
    """

    def __init__(self, app: FastAPI) -> None:
        self.port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        # Poll until the server's started flag flips, then a TCP-connect
        # probe confirms the listening socket is actually accepting.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", self.port), timeout=0.1
                    ):
                        return
                except OSError:
                    pass
            time.sleep(0.02)
        raise RuntimeError("uvicorn did not start in time")

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=timeout)


@contextlib.contextmanager
def _running_server(app: FastAPI) -> Iterator[_RunningServer]:
    """Yield a started :class:`_RunningServer`; stop it on exit."""
    server = _RunningServer(app)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _parse_frame(block: str) -> dict[str, str]:
    """Parse a single SSE block (already stripped of trailing blank line)."""
    frame: dict[str, str] = {}
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        frame[field] = value.lstrip(" ")
    return frame


async def _read_one_frame(
    response: httpx.Response, *, timeout: float = 3.0
) -> dict[str, str]:
    """Read raw bytes until one non-comment SSE frame arrives."""
    buffer = ""

    async def _pump() -> dict[str, str]:
        nonlocal buffer
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, _, buffer = buffer.partition("\n\n")
                frame = _parse_frame(block)
                if frame:
                    return frame
                # Comment-only block (keepalive) — keep going.
        raise AssertionError("stream ended before a frame arrived")

    return await asyncio.wait_for(_pump(), timeout=timeout)


def _publish_after(host: ModuleHost, event: Event, delay: float = 0.05) -> threading.Thread:
    """Publish ``event`` from a background thread after ``delay`` seconds.

    The streaming generator runs on uvicorn's event loop in a *different*
    thread; publishing from a third thread exercises the
    ``call_soon_threadsafe`` path the production code uses when an
    EventBus publish originates from a non-asyncio thread.
    """

    def _go() -> None:
        time.sleep(delay)
        host.publish(event)

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Exceptions exist under the FullstackError hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_unknown_event_subscription_is_fullstack_error(self) -> None:
        from pymodules.contrib.fullstack import FullstackError

        assert issubclass(UnknownEventSubscription, FullstackError)

    def test_missing_outbound_policy_is_fullstack_error(self) -> None:
        from pymodules.contrib.fullstack import FullstackError

        assert issubclass(MissingOutboundPolicy, FullstackError)


# ---------------------------------------------------------------------------
# Auth failures short-circuit before subscription work
# ---------------------------------------------------------------------------


class TestAuth:
    def test_no_cookie_returns_401_with_challenge(self, jwt_provider) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app = _build_app(host, jwt_provider)
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events", params={"subscribe": "MessagePosted"}
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'
        finally:
            host.shutdown()

    def test_expired_cookie_returns_401_with_challenge(
        self, jwt_provider
    ) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app = _build_app(host, jwt_provider)
            expired = _make_expired_jwt(jwt_provider, {"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "MessagePosted"},
                cookies={"pymodules_access": expired},
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == 'Cookie realm="pymodules"'
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Subscription validation (non-streaming — TestClient is fine here)
# ---------------------------------------------------------------------------


class TestSubscriptionValidation:
    def test_unknown_event_returns_400(self, jwt_provider, issue_token_sync) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app = _build_app(host, jwt_provider)
            token = issue_token_sync({"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "NoSuchEvent"},
                cookies={"pymodules_access": token},
            )
            assert response.status_code == 400
            body = response.json()
            payload = body.get("detail", body)
            assert payload == {"error": "unknown_event", "event": "NoSuchEvent"}
        finally:
            host.shutdown()

    def test_missing_outbound_policy_returns_400(
        self, jwt_provider, issue_token_sync
    ) -> None:
        host = ModuleHost()
        host.register(UnpoliciedModule())
        try:
            app = _build_app(host, jwt_provider)
            token = issue_token_sync({"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "UnpoliciedEvent"},
                cookies={"pymodules_access": token},
            )
            assert response.status_code == 400
            payload = response.json().get("detail", response.json())
            assert payload == {
                "error": "no_outbound_policy",
                "event": "UnpoliciedEvent",
            }
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# End-to-end streaming via a real uvicorn server
# ---------------------------------------------------------------------------


class TestAlwaysTruePolicy:
    async def test_event_reaches_client_in_sse_wire_format(
        self, jwt_provider, issue_token_async
    ) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u-1", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith(
                            "text/event-stream"
                        )

                        # Wait until our subscription is wired before publishing.
                        for _ in range(100):
                            if host.event_bus.subscriber_count(MessagePosted) >= 1:
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(
                            host, MessagePosted(tenant_id="acme", body="hello")
                        )

                        frame = await _read_one_frame(response)
                        assert frame["event"] == "MessagePosted"
                        assert frame["id"] == "1"
                        payload = json.loads(frame["data"])
                        assert payload["tenant_id"] == "acme"
                        assert payload["body"] == "hello"
        finally:
            host.shutdown()


class TestTenantMatchPolicy:
    async def test_only_matching_tenant_receives_event(
        self, jwt_provider, issue_token_async
    ) -> None:
        host = ModuleHost()
        host.register(TenantMatchModule())
        try:
            app = _build_app(host, jwt_provider)
            token_acme = await issue_token_async(
                {"sub": "u-acme", "tenant_id": "acme"}
            )

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted"},
                        cookies={"pymodules_access": token_acme},
                    ) as response:
                        for _ in range(100):
                            if host.event_bus.subscriber_count(MessagePosted) >= 1:
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(
                            host, MessagePosted(tenant_id="other", body="x"), 0.02
                        )
                        _publish_after(
                            host, MessagePosted(tenant_id="acme", body="y"), 0.1
                        )

                        frame = await _read_one_frame(response, timeout=3.0)
                        payload = json.loads(frame["data"])
                        # The "other" tenant's Event was filtered by the
                        # policy; the first frame this client sees is the
                        # "acme" Event.
                        assert payload["tenant_id"] == "acme"
                        assert payload["body"] == "y"
        finally:
            host.shutdown()

    async def test_cross_tenant_client_sees_nothing(
        self, jwt_provider, issue_token_async
    ) -> None:
        """An ``other``-tenant client subscribes; we publish a tenant-``acme``
        Event. The policy drops it for this client; no frame arrives within
        the timeout window."""
        host = ModuleHost()
        host.register(TenantMatchModule())
        try:
            app = _build_app(host, jwt_provider)
            token_other = await issue_token_async(
                {"sub": "u-other", "tenant_id": "other"}
            )

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted"},
                        cookies={"pymodules_access": token_other},
                    ) as response:
                        for _ in range(100):
                            if host.event_bus.subscriber_count(MessagePosted) >= 1:
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(
                            host, MessagePosted(tenant_id="acme", body="x")
                        )
                        with pytest.raises(asyncio.TimeoutError):
                            await _read_one_frame(response, timeout=0.5)
        finally:
            host.shutdown()


class TestMultipleSubscriptions:
    async def test_two_event_classes_in_one_subscribe(
        self, jwt_provider, issue_token_async
    ) -> None:
        host = ModuleHost()
        host.register(TwoEventsModule())
        try:
            app = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted,OrderPlaced"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        for _ in range(100):
                            if (
                                host.event_bus.subscriber_count(MessagePosted) >= 1
                                and host.event_bus.subscriber_count(OrderPlaced) >= 1
                            ):
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(
                            host,
                            MessagePosted(tenant_id="acme", body="m"),
                            0.02,
                        )
                        _publish_after(
                            host,
                            OrderPlaced(tenant_id="acme", order_id="o-1"),
                            0.05,
                        )

                        seen: dict[str, dict[str, str]] = {}

                        async def _drain_until_both() -> None:
                            buffer = ""
                            async for chunk in response.aiter_text():
                                buffer += chunk
                                while "\n\n" in buffer:
                                    block, _, buffer = buffer.partition("\n\n")
                                    frame = _parse_frame(block)
                                    if not frame:
                                        continue
                                    seen[frame["event"]] = frame
                                    if {"MessagePosted", "OrderPlaced"} <= seen.keys():
                                        return

                        await asyncio.wait_for(_drain_until_both(), timeout=5.0)

                        assert "MessagePosted" in seen
                        assert "OrderPlaced" in seen
                        assert (
                            json.loads(seen["MessagePosted"]["data"])["body"]
                            == "m"
                        )
                        assert (
                            json.loads(seen["OrderPlaced"]["data"])["order_id"]
                            == "o-1"
                        )
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Per-connection error isolation
# ---------------------------------------------------------------------------


class TestPerConnectionErrorIsolation:
    async def test_raising_policy_does_not_break_sibling_connection(
        self, jwt_provider, issue_token_async
    ) -> None:
        """ADR-0007's error-isolation guarantee, carried one layer up: a
        raising outbound policy for one connection must not affect a
        sibling connection on the same Event class.

        Setup: the policy raises iff ``client.user_id == "bomb-user"``.
        Two streams open in parallel; we publish one Event and confirm
        the "good" stream receives it while the "bomb" stream sees
        nothing but remains healthy (no closed response, no 500).
        """
        host = ModuleHost()
        host.register(RaisingPolicyModule())
        try:
            app = _build_app(host, jwt_provider)
            good_token = await issue_token_async(
                {"sub": "good-user", "tenant_id": "acme"}
            )
            bomb_token = await issue_token_async(
                {"sub": "bomb-user", "tenant_id": "acme"}
            )

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as good_client, \
                        httpx.AsyncClient(base_url=server.base_url) as bomb_client:
                    async with good_client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted"},
                        cookies={"pymodules_access": good_token},
                    ) as good_resp, bomb_client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted"},
                        cookies={"pymodules_access": bomb_token},
                    ) as bomb_resp:
                        assert good_resp.status_code == 200
                        assert bomb_resp.status_code == 200

                        # Both subscriptions wired before publishing.
                        for _ in range(200):
                            if host.event_bus.subscriber_count(MessagePosted) >= 2:
                                break
                            await asyncio.sleep(0.02)
                        assert host.event_bus.subscriber_count(MessagePosted) >= 2

                        _publish_after(
                            host,
                            MessagePosted(tenant_id="acme", body="hi"),
                            delay=0.05,
                        )

                        good_frame = await _read_one_frame(good_resp, timeout=3.0)
                        assert good_frame["event"] == "MessagePosted"
                        assert json.loads(good_frame["data"])["body"] == "hi"

                        # The bomb stream's policy raised; the frame is
                        # dropped *for that connection only* and the
                        # stream is still live (a timeout from our
                        # helper, not a ``ConnectionClosed`` / 500).
                        with pytest.raises(asyncio.TimeoutError):
                            await _read_one_frame(bomb_resp, timeout=0.5)
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Client-disconnect cleanup
# ---------------------------------------------------------------------------


class TestDisconnectCleanup:
    async def test_disconnect_unsubscribes_all_callbacks(
        self, jwt_provider, issue_token_async
    ) -> None:
        """The streaming generator's ``finally`` must unsubscribe every
        callback. We open a stream that subscribes to two Event classes,
        confirm the EventBus subscriber count rose by one for each, close
        the stream, and poll until both counts return to baseline.
        """
        host = ModuleHost()
        host.register(TwoEventsModule())
        try:
            app = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u", "tenant_id": "acme"})

            baseline_msg = host.event_bus.subscriber_count(MessagePosted)
            baseline_ord = host.event_bus.subscriber_count(OrderPlaced)

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "MessagePosted,OrderPlaced"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        assert response.status_code == 200
                        for _ in range(200):
                            if (
                                host.event_bus.subscriber_count(MessagePosted)
                                == baseline_msg + 1
                                and host.event_bus.subscriber_count(OrderPlaced)
                                == baseline_ord + 1
                            ):
                                break
                            await asyncio.sleep(0.02)
                        assert (
                            host.event_bus.subscriber_count(MessagePosted)
                            == baseline_msg + 1
                        )
                        assert (
                            host.event_bus.subscriber_count(OrderPlaced)
                            == baseline_ord + 1
                        )

                    # ``client.stream``'s ``async with`` exit closes the
                    # response and signals the server to cancel the
                    # streaming task; its ``finally`` runs on uvicorn's
                    # loop. Poll until cleanup completes.
                    for _ in range(200):
                        if (
                            host.event_bus.subscriber_count(MessagePosted)
                            == baseline_msg
                            and host.event_bus.subscriber_count(OrderPlaced)
                            == baseline_ord
                        ):
                            break
                        await asyncio.sleep(0.02)

                    assert (
                        host.event_bus.subscriber_count(MessagePosted)
                        == baseline_msg
                    )
                    assert (
                        host.event_bus.subscriber_count(OrderPlaced)
                        == baseline_ord
                    )
        finally:
            host.shutdown()
