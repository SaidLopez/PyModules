"""Tests for SSE observability metrics + graceful-shutdown drain (issue #8).

Each acceptance criterion gets its own focused test:

- ``connections_opened`` increments on accepted streams (not on rejected
  subscription validation / 401s).
- ``events_pushed`` increments per frame written to the wire and does
  *not* increment when an Outbound policy denies the Event for a client
  (the positive + negative test live as one parametrised pair).
- ``denials_unknown_event`` increments on the 400 unknown-event path.
- ``denials_no_outbound_policy`` increments on the 400 no-policy path.
- ``denials_unauthenticated`` increments on the 401 cookie-auth path.
- The shutdown-drain happy path: two streams connected, call
  ``host.shutdown()``, both connections close within the grace window
  and the EventBus subscriber count returns to zero.
- A new connection arriving while shutdown is in flight gets 503.

Harness re-use
--------------

The streaming tests reuse the ``_RunningServer`` / ``_running_server``
context manager from ``test_sse.py`` — same pattern: a real
:class:`uvicorn.Server` on a free port in a daemon thread, consumed via
``httpx.AsyncClient.stream``. ``TestClient`` won't do here because it
buffers the full response body before returning.

Non-streaming tests (the counter-on-validation-error cases) use
:class:`fastapi.testclient.TestClient` since those responses complete
immediately.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymodules import Event, Module, ModuleHost
from pymodules.contrib.fullstack import (
    ClientContext,
    build_sse_router,
    make_cookie_auth_dependency,
    outbound_policy,
    register_with_host,
)

# Re-use the uvicorn-in-a-thread harness from the issue #7 tests.
from tests.contrib.fullstack.test_sse import (
    _publish_after,
    _read_one_frame,
    _running_server,
)

# ---------------------------------------------------------------------------
# Synthetic Events + Modules
# ---------------------------------------------------------------------------


@dataclass
class Pinged(Event):
    tenant_id: str = ""
    payload: str = ""
    name: str = "pinged"


class AlwaysTrueModule(Module):
    """Permissive policy: every client passes."""

    published_events = (Pinged,)

    @outbound_policy(Pinged)
    def gate(self, event: Pinged, client: ClientContext) -> bool:
        return True


class DenyAllModule(Module):
    """Policy returns ``False`` for every Event — exercises the
    "denied-by-policy does not increment ``events_pushed``" AC.
    """

    published_events = (Pinged,)

    @outbound_policy(Pinged)
    def deny(self, event: Pinged, client: ClientContext) -> bool:
        return False


class UnpoliciedModule(Module):
    """Declares ``Pinged`` as published with no policy registered."""

    published_events = (Pinged,)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_sse.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_provider():
    from pymodules.contrib.api.auth import JWTAuthProvider, JWTSettings

    settings = JWTSettings(
        secret_key="test-secret-key-for-sse-obs-tests-1234567890",
        access_token_expire_minutes=30,
    )
    return JWTAuthProvider(settings)


@pytest.fixture
def issue_token_async(jwt_provider):
    async def _issue(claims: dict[str, Any]) -> str:
        return await jwt_provider.create_token(claims)

    return _issue


@pytest.fixture
def issue_token_sync(jwt_provider):
    def _issue(claims: dict[str, Any]) -> str:
        return asyncio.get_event_loop().run_until_complete(jwt_provider.create_token(claims))

    return _issue


def _build_app(host: ModuleHost, jwt_provider):
    cookie_auth = make_cookie_auth_dependency(jwt_provider)
    router = build_sse_router(host, cookie_auth_dependency=cookie_auth)
    app = FastAPI()
    app.include_router(router)
    return app, router


# ---------------------------------------------------------------------------
# Router surface: metrics live on the returned router
# ---------------------------------------------------------------------------


class TestMetricsSurface:
    def test_router_exposes_metrics_dataclass_with_all_counters_at_zero(self, jwt_provider) -> None:
        from pymodules.contrib.fullstack import SSEMetrics

        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            _, router = _build_app(host, jwt_provider)
            assert isinstance(router.metrics, SSEMetrics)
            assert router.metrics.connections_opened == 0
            assert router.metrics.events_pushed == 0
            assert router.metrics.denials_unknown_event == 0
            assert router.metrics.denials_no_outbound_policy == 0
            assert router.metrics.denials_unauthenticated == 0
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Denial counters (non-streaming — TestClient is fine)
# ---------------------------------------------------------------------------


class TestDenialCounters:
    def test_unauthenticated_denial_increments_counter(self, jwt_provider) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            client = TestClient(app)
            response = client.get("/__pymodules__/events", params={"subscribe": "Pinged"})
            assert response.status_code == 401
            assert router.metrics.denials_unauthenticated == 1
            # And we did NOT count this as an opened connection.
            assert router.metrics.connections_opened == 0
        finally:
            host.shutdown()

    def test_unknown_event_denial_increments_counter(self, jwt_provider, issue_token_sync) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            token = issue_token_sync({"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "NoSuchEvent"},
                cookies={"pymodules_access": token},
            )
            assert response.status_code == 400
            assert router.metrics.denials_unknown_event == 1
            assert router.metrics.connections_opened == 0
        finally:
            host.shutdown()

    def test_no_outbound_policy_denial_increments_counter(
        self, jwt_provider, issue_token_sync
    ) -> None:
        host = ModuleHost()
        host.register(UnpoliciedModule())
        try:
            app, router = _build_app(host, jwt_provider)
            token = issue_token_sync({"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "Pinged"},
                cookies={"pymodules_access": token},
            )
            assert response.status_code == 400
            assert router.metrics.denials_no_outbound_policy == 1
            assert router.metrics.connections_opened == 0
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Streaming-path counters: connections_opened + events_pushed
# ---------------------------------------------------------------------------


class TestStreamingCounters:
    async def test_connections_opened_increments_on_accepted_stream(
        self, jwt_provider, issue_token_async
    ) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u-1", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "Pinged"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        assert response.status_code == 200
                        # Subscription wired -> counter has incremented.
                        for _ in range(100):
                            if host.event_bus.subscriber_count(Pinged) >= 1:
                                break
                            await asyncio.sleep(0.02)
                        assert router.metrics.connections_opened == 1
        finally:
            host.shutdown()

    async def test_events_pushed_increments_per_frame_written(
        self, jwt_provider, issue_token_async
    ) -> None:
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u-1", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "Pinged"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        for _ in range(100):
                            if host.event_bus.subscriber_count(Pinged) >= 1:
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(host, Pinged(tenant_id="acme", payload="a"))
                        frame = await _read_one_frame(response)
                        assert frame["event"] == "Pinged"
                        assert json.loads(frame["data"])["payload"] == "a"

                        # One frame written -> exactly one push.
                        assert router.metrics.events_pushed == 1
        finally:
            host.shutdown()

    async def test_events_pushed_does_not_increment_when_policy_denies(
        self, jwt_provider, issue_token_async
    ) -> None:
        """AC: events_pushed must NOT increment when a policy denies."""
        host = ModuleHost()
        host.register(DenyAllModule())
        try:
            app, router = _build_app(host, jwt_provider)
            token = await issue_token_async({"sub": "u-1", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with httpx.AsyncClient(base_url=server.base_url) as client:
                    async with client.stream(
                        "GET",
                        "/__pymodules__/events",
                        params={"subscribe": "Pinged"},
                        cookies={"pymodules_access": token},
                    ) as response:
                        assert response.status_code == 200
                        for _ in range(100):
                            if host.event_bus.subscriber_count(Pinged) >= 1:
                                break
                            await asyncio.sleep(0.02)

                        _publish_after(host, Pinged(tenant_id="acme", payload="dropped"))

                        # Read attempt times out — policy dropped the event.
                        with pytest.raises(asyncio.TimeoutError):
                            await _read_one_frame(response, timeout=0.5)

                        # Connection still counted as opened...
                        assert router.metrics.connections_opened == 1
                        # ...but no frame was written, so events_pushed
                        # stays at zero.
                        assert router.metrics.events_pushed == 0
        finally:
            host.shutdown()


# ---------------------------------------------------------------------------
# Graceful shutdown drain
# ---------------------------------------------------------------------------


class TestShutdownDrain:
    async def test_host_shutdown_closes_connected_streams_within_grace(
        self, jwt_provider, issue_token_async
    ) -> None:
        """Two clients connected; ``host.shutdown()`` closes both within
        the grace window and releases the EventBus subscriptions."""
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            register_with_host(host, router, shutdown_grace=2.0)
            token = await issue_token_async({"sub": "u-1", "tenant_id": "acme"})

            with _running_server(app) as server:
                async with (
                    httpx.AsyncClient(base_url=server.base_url) as c1,
                    httpx.AsyncClient(base_url=server.base_url) as c2,
                ):
                    async with (
                        c1.stream(
                            "GET",
                            "/__pymodules__/events",
                            params={"subscribe": "Pinged"},
                            cookies={"pymodules_access": token},
                        ) as r1,
                        c2.stream(
                            "GET",
                            "/__pymodules__/events",
                            params={"subscribe": "Pinged"},
                            cookies={"pymodules_access": token},
                        ) as r2,
                    ):
                        assert r1.status_code == 200
                        assert r2.status_code == 200

                        # Both subscriptions wired before we shut down.
                        for _ in range(200):
                            if host.event_bus.subscriber_count(Pinged) >= 2:
                                break
                            await asyncio.sleep(0.02)
                        assert host.event_bus.subscriber_count(Pinged) == 2
                        assert router.metrics.connections_opened == 2

                        # Trigger shutdown from a worker thread so the
                        # asyncio event loop can keep servicing the
                        # streaming generators while drain proceeds.
                        # ``host.shutdown`` is sync; calling it from the
                        # test's own loop would block this coroutine
                        # before the streams could exit.
                        shutdown_done = threading.Event()

                        def _do_shutdown() -> None:
                            host.shutdown()
                            shutdown_done.set()

                        t = threading.Thread(target=_do_shutdown, daemon=True)
                        start = time.monotonic()
                        t.start()

                        # Drain the streaming bodies — once the server
                        # closes the connection, ``aiter_text`` will
                        # exit cleanly. Use a short timeout so a
                        # regression where shutdown never fires the
                        # signal is obvious.
                        async def _drain_to_end(resp: httpx.Response) -> None:
                            async for _ in resp.aiter_text():
                                pass

                        await asyncio.wait_for(
                            asyncio.gather(_drain_to_end(r1), _drain_to_end(r2)),
                            timeout=3.0,
                        )

                        # Shutdown thread should have finished too.
                        assert shutdown_done.wait(timeout=3.0)
                        elapsed = time.monotonic() - start
                        # Sanity: drain shouldn't run for the *entire*
                        # grace window — connections close as soon as
                        # the asyncio event fires, well under 2s.
                        assert elapsed < 3.0

            # No leaked EventBus subscriptions.
            assert host.event_bus.subscriber_count(Pinged) == 0
        finally:
            # ``host.shutdown`` already ran inside the test; calling it
            # again is safe (it's idempotent in practice — re-running
            # against an empty Module list is a no-op).
            host.shutdown()

    def test_new_connection_during_shutdown_gets_503(self, jwt_provider, issue_token_sync) -> None:
        """Once shutdown has flipped the coordinator, a fresh GET must
        be rejected with 503 — the router stops accepting new streams.

        We exercise the shutting-down state without actually tearing
        the host down: signal directly on the coordinator so we can
        still issue the test request synchronously through TestClient.
        """
        host = ModuleHost()
        host.register(AlwaysTrueModule())
        try:
            app, router = _build_app(host, jwt_provider)
            # Flip the flag without invoking ``host.shutdown`` (which
            # would tear down the module registrations the request
            # path still needs).
            router._sse_shutdown_coordinator.signal_shutdown()

            token = issue_token_sync({"sub": "u"})
            client = TestClient(app)
            response = client.get(
                "/__pymodules__/events",
                params={"subscribe": "Pinged"},
                cookies={"pymodules_access": token},
            )
            assert response.status_code == 503
            payload = response.json().get("detail", response.json())
            assert payload == {"error": "shutting_down"}
            # 503 is a shutdown signal, not a counted denial.
            assert router.metrics.connections_opened == 0
        finally:
            host.shutdown()
