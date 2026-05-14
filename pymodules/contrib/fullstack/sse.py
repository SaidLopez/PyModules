"""Server-Sent Events push endpoint for the fullstack contrib (issue #7 / ADR-0009).

This module mounts a single FastAPI route — ``/__pymodules__/events`` by
default — that an authenticated browser client opens with ``EventSource``
to receive Events published on the host's :class:`pymodules.EventBus`.

Architectural placement
-----------------------

The SSE layer sits **above** the EventBus, not inside it (ADR-0007). The
flow per connection is:

1. Cookie auth shim (``make_cookie_auth_dependency``) validates the JWT
   cookie and constructs a :class:`ClientContext`. A missing or expired
   cookie short-circuits with HTTP 401 + ``WWW-Authenticate: Cookie``
   before any subscription work happens.
2. The ``subscribe`` query parameter (``?subscribe=A,B,C``) is parsed and
   each name is resolved against the host's declared ``published_events``:
   - Unknown name -> :class:`UnknownEventSubscription` -> HTTP 400.
   - Known + no Outbound policy -> :class:`MissingOutboundPolicy` -> HTTP 400.
3. A per-connection :class:`asyncio.Queue` is opened. For each subscribed
   Event class the writer registers a sync callback on
   ``host.event_bus.subscribe(EventCls, _enqueue)`` that schedules the
   Event onto the connection's event loop via ``call_soon_threadsafe``.
4. The streaming response loop pulls Events off the queue, calls
   ``host.outbound_policies.apply(event, client_ctx)`` for each, and emits
   ``event:`` / ``id:`` / ``data:`` SSE frames for events that pass.
   ``apply()`` is wrapped in ``try/except`` so a raising policy on one
   connection only drops the offending Event for *that* connection — sibling
   connections subscribed to the same Event class are unaffected (ADR-0007
   error isolation, carried up one layer).
5. On client disconnect (queue iteration cancelled, ``is_disconnected``
   returns ``True``, or the response is closed by the transport) the
   ``finally`` block unsubscribes every callback this connection registered
   on the EventBus so subscription count returns to the baseline.

Event-name -> class resolution
------------------------------

Resolved by walking ``host.modules`` and collecting each Module's declared
``published_events`` ClassVar (the same source AsyncAPI emission and the
``@outbound_policy`` wiring use). This is the publication contract — a name
that isn't on any Module's ``published_events`` is by definition not part
of the host's outbound surface, so a 400 is the right answer. We do not
walk arbitrary Event subclasses registered on the EventBus, because internal
intra-host Events that aren't browser-facing should never become reachable
just by guessing their class name.

Wire format
-----------

Standard SSE. Per ``apply()``-allowed Event::

    event: <EventClassName>\\n
    id: <monotonic-id-per-connection>\\n
    data: <json-via-dataclasses.asdict>\\n
    \\n

Last-Event-Id replay is out of scope (PRD calls it "broker territory").

Observability (issue #8)
------------------------

The router carries an :class:`SSEMetrics` on ``router.metrics`` —
five integer counters following the same "own your counters directly"
shape that :class:`pymodules.tracing.MetricsMiddleware` uses for the
dispatch surface. ``connections_opened`` ticks once per accepted
stream; ``events_pushed`` ticks per frame written (not per Event
denied by policy); the three ``denials_*`` counters break down the
rejection paths (``unknown_event``, ``no_outbound_policy``,
``unauthenticated``). The auth-denial count is wired by wrapping the
cookie auth dependency — FastAPI raises before the endpoint body, so
counting from inside the body is impossible.

Graceful shutdown (issue #8)
----------------------------

:func:`register_with_host` monkey-patches ``host.shutdown`` to first
flip an internal "shutting down" flag (which causes new connections to
get 503) and signal every connected stream via a per-connection
:class:`asyncio.Event`, then wait up to ``shutdown_grace`` seconds for
the active count to drain before delegating to the original
``shutdown``. The streaming loop races its queue ``get`` against the
shutdown event with :func:`asyncio.wait`, so drain happens immediately
on shutdown — not on the next 15-second keepalive tick. The same
pattern manifest #6 uses (a contrib-owned monkey-patch with an
idempotency marker on the host) keeps the seam out of core
(ADR-0002).

What this module deliberately does *not* do
-------------------------------------------

- It does not modify ``pymodules.eventbus`` (ADR-0007 contract is sacred).
- It does not introduce a new ``ClientContext`` shape — it consumes the
  one shipped by the cookie auth shim.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from .exceptions import FullstackError, MissingOutboundPolicy, UnknownEventSubscription

if TYPE_CHECKING:
    from pymodules.host import ModuleHost
    from pymodules.interfaces import Event

    from .client_context import ClientContext


sse_logger = logging.getLogger("pymodules.contrib.fullstack.sse")


# Default mount path. ADR-0009 / PRD pin ``/__pymodules__/events``; the
# factory accepts a ``path`` override for hosts that namespace differently.
DEFAULT_SSE_PATH = "/__pymodules__/events"

# Media type pinned by the SSE spec.
_SSE_MEDIA_TYPE = "text/event-stream"

# Marker attribute the shutdown-hook helper writes on a host so a repeat
# call is a no-op (mirrors ``_INVALIDATOR_ATTACHED_ATTR`` in ``manifest``).
_SSE_SHUTDOWN_HOOK_ATTR = "__pymodules_sse_shutdown_hook_attached__"


@dataclass
class SSEMetrics:
    """In-memory counters for the SSE router.

    Owned directly by the router (set as ``router.metrics``) and inspected
    by user code or tests. Mirrors the "own your counters" pattern that
    :class:`pymodules.tracing.MetricsMiddleware` uses for the dispatch
    surface — no parallel observability silo, no external registry.

    Attributes:
        connections_opened: Total SSE connections accepted by the
            endpoint (after auth + subscription validation pass). Not
            incremented for rejected validation or 401s.
        events_pushed: Total SSE frames written to the wire across all
            connections. **Not** incremented when an Outbound policy
            denies the Event for a given client.
        denials_unknown_event: Total 400 responses for ``unknown_event``.
        denials_no_outbound_policy: Total 400 responses for
            ``no_outbound_policy``.
        denials_unauthenticated: Total 401 responses from the cookie
            auth dependency. Counted by wrapping the dependency below.
    """

    connections_opened: int = 0
    events_pushed: int = 0
    denials_unknown_event: int = 0
    denials_no_outbound_policy: int = 0
    denials_unauthenticated: int = 0


class _ShutdownCoordinator:
    """Thread-safe shutdown coordinator for the SSE router.

    The streaming generators run on the FastAPI/uvicorn event loop;
    ``host.shutdown()`` is a synchronous call typically invoked from the
    main thread. Two cross-thread concerns:

    1. The streaming generator needs to be told "exit now" without
       waiting for the next 15-second keepalive tick. We achieve this
       with one :class:`asyncio.Event` per connection, captured along
       with the loop the connection runs on; signalling crosses the
       thread boundary via ``loop.call_soon_threadsafe(event.set)``.
    2. The wrapping shutdown needs to know when all in-flight
       connections have drained so it can return within the grace
       window. We track an integer counter guarded by a
       :class:`threading.Lock`; the generators decrement it in their
       ``finally`` blocks.

    The ``_shutting_down`` flag is read by the router endpoint (sync
    HTTP handler path) to reject new connections with 503. A plain
    ``bool`` is fine — Python's GIL makes the read atomic and we never
    see a stale ``False`` here that would matter (worst case: one
    extra connection sneaks past the check on a race; it then sees
    the shutdown event fire on its first iteration and exits cleanly).
    """

    def __init__(self) -> None:
        self._shutting_down = False
        self._lock = threading.Lock()
        self._active_connections = 0
        # One ``(loop, event)`` per connection so ``signal_shutdown``
        # can wake every generator regardless of which loop hosts it.
        # In practice all connections share the uvicorn loop, but
        # nothing in the protocol depends on that — multi-loop setups
        # (e.g. a future split-process embedding) keep working.
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def active_connections(self) -> int:
        with self._lock:
            return self._active_connections

    def register_connection(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        """Track a new connection's loop+event and bump the active counter.

        Called from inside the streaming generator on the connection's
        own loop. If shutdown is already in flight by the time this
        runs, immediately set the event so the generator exits on its
        first iteration.
        """
        with self._lock:
            self._active_connections += 1
            self._waiters.append((loop, event))
            already_shutting = self._shutting_down
        if already_shutting:
            event.set()

    def unregister_connection(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        """Drop a connection's tracking entry on disconnect."""
        with self._lock:
            self._active_connections = max(0, self._active_connections - 1)
            try:
                self._waiters.remove((loop, event))
            except ValueError:
                # Already removed; harmless.
                pass

    def signal_shutdown(self) -> None:
        """Flip the shutting-down flag and wake every connected generator.

        Safe to call from any thread. Each ``event.set`` is bounced onto
        the owning loop with ``call_soon_threadsafe``; if a loop has
        already closed (connection torn down concurrently) the
        ``RuntimeError`` is swallowed — that connection is gone anyway.
        """
        with self._lock:
            self._shutting_down = True
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # Loop closed mid-shutdown; nothing to wake.
                pass

    def wait_for_drain(self, timeout: float, poll: float = 0.02) -> bool:
        """Block (synchronously) until ``active_connections`` hits zero.

        Returns ``True`` if drain completed within the timeout, ``False``
        if the timeout fired with connections still in flight. Either
        way, the caller (``register_with_host`` below) continues with
        the rest of ``host.shutdown``.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_connections == 0:
                return True
            time.sleep(poll)
        return self.active_connections == 0


def _build_event_name_index(host: ModuleHost) -> dict[str, type[Event]]:
    """Build a ``{EventClassName: EventCls}`` index from declared publications.

    Walks ``host.modules`` and collects each Module's ``published_events``
    ClassVar — the same publication contract AsyncAPI emission and the
    ``@outbound_policy`` decorator consult. A class declared by two Modules
    resolves to either entry (they are the same class, so the value is
    identical). A name that isn't on any Module's tuple does not appear
    in the index, and an SSE subscription naming it is rejected with
    :class:`UnknownEventSubscription`.
    """
    index: dict[str, type[Event]] = {}
    for module in host.modules:
        for event_cls in getattr(type(module), "published_events", ()):
            index[event_cls.__name__] = event_cls
    return index


def _parse_subscribe_param(raw: str) -> list[str]:
    """Split ``?subscribe=A,B,C`` into ``["A", "B", "C"]``.

    Empty entries (from trailing commas, double commas, or pure whitespace)
    are dropped — an empty string is not a valid Event class name, so
    routing it through the unknown-name path would surface a confusing
    ``{"error": "unknown_event", "event": ""}`` instead of a real bug. A
    request with no usable names raises ``HTTPException(400)`` upstream.
    """
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def _format_sse_frame(event_cls_name: str, event_id: int, payload: dict[str, Any]) -> bytes:
    """Build a single SSE frame as raw bytes.

    Wire format (mirroring the docstring / PRD):

        event: <EventClassName>\\n
        id: <event_id>\\n
        data: <json-payload>\\n
        \\n

    JSON serialization uses ``json.dumps`` with ``default=str`` so non-JSON-
    native scalars in the dataclass payload (e.g. ``datetime`` if a Module
    declares one) still serialize rather than raising mid-stream.
    """
    data = json.dumps(payload, default=str, separators=(",", ":"))
    # ``\n`` line endings per the SSE spec. CRLF is also allowed by the
    # spec but plain LF is what httpx-sse / browser EventSource emit and
    # consume in practice.
    return (f"event: {event_cls_name}\nid: {event_id}\ndata: {data}\n\n").encode()


def build_sse_router(
    host: ModuleHost,
    *,
    cookie_auth_dependency: Callable[..., Any],
    path: str = DEFAULT_SSE_PATH,
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` exposing the SSE push endpoint.

    Args:
        host: The :class:`ModuleHost` whose Modules' ``published_events``
            define the subscribable surface and whose ``event_bus`` is
            the fan-out source.
        cookie_auth_dependency: A FastAPI dependency callable (typically
            the result of :func:`make_cookie_auth_dependency`) that
            authenticates the connection via cookie and returns a
            :class:`ClientContext`. Passed in (rather than constructed
            inline) so the caller controls the underlying ``AuthProvider``
            and cookie name.
        path: Mount path for the endpoint. Defaults to ``/__pymodules__/events``.

    Returns:
        An ``APIRouter`` with a single ``GET`` route at ``path``. Mount it
        on the application with ``app.include_router(router)``.

    Endpoint behaviour
    ------------------

    - Missing / expired cookie -> 401 (raised by the dependency).
    - ``?subscribe=`` query parameter is required, comma-separated.
    - Unknown Event name -> 400 ``{"error": "unknown_event", "event": "<name>"}``.
    - Known Event with no Outbound policy -> 400
      ``{"error": "no_outbound_policy", "event": "<name>"}``.
    - Valid subscription -> ``200`` streaming response with
      ``Content-Type: text/event-stream``; one SSE frame per published
      Event that ``host.outbound_policies.apply(event, ctx)`` admits.
    """
    router = APIRouter()
    metrics = SSEMetrics()
    coordinator = _ShutdownCoordinator()

    # Wrap the auth dependency so a 401 from the cookie shim increments
    # ``denials_unauthenticated``. We can't count 401s from inside the
    # endpoint body because FastAPI runs dependencies *before* the body
    # — a raised ``HTTPException`` short-circuits the request and the
    # body never executes. Wrapping the dependency keeps the count
    # scoped to the SSE route (not a global FastAPI middleware that
    # would also count 401s on unrelated routes).
    async def _counting_auth(*args: Any, **kwargs: Any) -> Any:
        try:
            result = cookie_auth_dependency(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except HTTPException as exc:
            if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                metrics.denials_unauthenticated += 1
            raise

    # Bind a unique name on the wrapper so FastAPI's dependency cache /
    # signature inspection don't conflate it with anything else; the
    # ``__signature__`` mirror keeps any Depends-injected sub-deps the
    # original might have declared working (FastAPI introspects the
    # callable's signature to wire those). The cookie auth dependency
    # currently takes only a single ``Request`` param; copying the
    # signature wholesale is the safe play.
    import inspect as _inspect

    try:
        _counting_auth.__signature__ = _inspect.signature(cookie_auth_dependency)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        # Builtins / C-coded callables: FastAPI will fall back to the
        # bare ``*args, **kwargs`` signature, which is fine for the
        # cookie shim which takes only a Request.
        pass

    @router.get(path)
    async def sse_events(
        request: Request,
        subscribe: str = Query(
            ...,
            description=(
                "Comma-separated list of Event class names to subscribe to. "
                "Every name must be on a registered Module's "
                "``published_events`` and have an Outbound policy registered."
            ),
        ),
        client_ctx: ClientContext = Depends(_counting_auth),
    ) -> StreamingResponse:
        # Reject new connections during shutdown so we don't accept a
        # client only to immediately drop it. 503 is the standard
        # "shutting down" signal browsers / proxies can retry against.
        if coordinator.shutting_down:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "shutting_down"},
            )
        # ---- Subscription validation -----------------------------------
        # Resolve every name in the ``?subscribe=`` list to its Event class
        # *before* we open the streaming response. A subscription failure
        # here surfaces as a structured 400 the JS layer can fail-fast on,
        # rather than an SSE connection that opens and then sees no traffic.
        names = _parse_subscribe_param(subscribe)
        if not names:
            # No usable names at all is treated as an unknown-event with
            # an empty name. Distinct from the "no subscribe param" case
            # which FastAPI's ``Query(...)`` short-circuits as 422.
            metrics.denials_unknown_event += 1
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "unknown_event", "event": ""},
            )

        event_index = _build_event_name_index(host)
        resolved: list[type[Event]] = []
        for name in names:
            event_cls = event_index.get(name)
            if event_cls is None:
                # Bubble through the typed exception in the log line, but
                # serve the structured 400 body the AC pins. We don't let
                # the exception propagate because FastAPI has no built-in
                # handler for ``FullstackError`` subclasses; the structured
                # body is more useful to the JS layer than a stack trace.
                exc: FullstackError = UnknownEventSubscription(name)
                sse_logger.info(
                    "Rejecting SSE subscription for unknown Event %r: %s",
                    name,
                    exc,
                )
                metrics.denials_unknown_event += 1
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "unknown_event", "event": name},
                )

            if not host.outbound_policies.has_policy(event_cls):
                exc = MissingOutboundPolicy(name)
                sse_logger.info(
                    "Rejecting SSE subscription for %r — no outbound policy: %s",
                    name,
                    exc,
                )
                metrics.denials_no_outbound_policy += 1
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "no_outbound_policy", "event": name},
                )

            resolved.append(event_cls)

        # ---- Per-connection fan-out ------------------------------------
        # Bounded queue so a slow client can't unboundedly buffer events.
        # ``maxsize=0`` would be unbounded; 1024 is plenty for the v1
        # use case (live UI updates) and gives us a natural backpressure
        # signal if a sub goes dark.
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
        loop = asyncio.get_running_loop()
        # Keep a stable record of every (EventCls, callback) pair we wired
        # so the ``finally`` block can unsubscribe them all even if the
        # streaming loop raises mid-flight.
        registrations: list[tuple[type[Event], Callable[[Event], None]]] = []

        def _make_enqueue(_event_cls: type[Event]) -> Callable[[Event], None]:
            """Build a sync EventBus callback that hands the Event to the queue.

            The EventBus may publish from any thread (sync ``publish`` runs
            inline on the publishing thread), so we use
            ``loop.call_soon_threadsafe`` to bounce the enqueue onto the
            connection's event loop. ``put_nowait`` rather than ``put`` so
            a full queue raises ``QueueFull`` (logged and dropped) instead
            of blocking the publisher.
            """

            def _enqueue(event: Event) -> None:
                def _push() -> None:
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        sse_logger.warning(
                            "SSE queue full for %s; dropping event %s",
                            _event_cls.__name__,
                            type(event).__name__,
                        )

                try:
                    loop.call_soon_threadsafe(_push)
                except RuntimeError:
                    # Loop closed mid-publish — connection is gone; drop
                    # silently. The ``finally`` in the streaming gen has
                    # already (or is about to) unsubscribe us.
                    pass

            return _enqueue

        for event_cls in resolved:
            callback = _make_enqueue(event_cls)
            host.event_bus.subscribe(event_cls, callback)
            registrations.append((event_cls, callback))

        # Per-connection shutdown event — set by the coordinator when
        # ``host.shutdown()`` runs so the streaming loop exits without
        # waiting for the 15-second keepalive timeout. Bind it to *this*
        # connection's loop so ``call_soon_threadsafe`` from the (sync)
        # shutdown thread reaches the right scheduler.
        shutdown_event = asyncio.Event()
        coordinator.register_connection(loop, shutdown_event)

        # Connection accepted: count it. AC pins this as "after auth +
        # subscription validation pass" — i.e. right here, not earlier.
        metrics.connections_opened += 1

        id_counter = count(1)

        async def event_stream() -> AsyncIterator[bytes]:
            """Drain the per-connection queue into SSE-formatted bytes.

            ``try/finally`` guarantees that *every* callback we registered
            on the EventBus is unsubscribed on the way out — natural
            client disconnect, raised exception, or task cancellation
            from the transport. This is the AC's "no leaked EventBus
            subscriptions" guarantee.
            """
            try:
                while True:
                    # Shutdown short-circuit: if the coordinator already
                    # flipped before we (re-)entered the loop, exit
                    # without one more wait cycle.
                    if shutdown_event.is_set():
                        break

                    # Race three signals: a new Event in the queue, the
                    # host-shutdown event, and the 15-second keepalive
                    # tick. ``asyncio.wait`` with FIRST_COMPLETED gives
                    # us the multiplexed wake-up; whichever fires first
                    # decides the next branch. Pending tasks are
                    # cancelled so the queue task doesn't leak a
                    # half-consumed ``get`` across iterations.
                    queue_task = asyncio.ensure_future(queue.get())
                    shutdown_task = asyncio.ensure_future(shutdown_event.wait())
                    try:
                        done, pending = await asyncio.wait(
                            {queue_task, shutdown_task},
                            timeout=15.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        # Always clean up whichever task didn't win.
                        # Cancelling the queue task is fine — it hasn't
                        # consumed an item yet (``get`` either suspends
                        # or completes atomically with the dequeue).
                        for task in (queue_task, shutdown_task):
                            if not task.done():
                                task.cancel()

                    if shutdown_task in done:
                        # Host is shutting down; drop out cleanly so the
                        # ``finally`` block unsubscribes and decrements
                        # the coordinator's active count.
                        break

                    if not done:
                        # Keepalive tick: neither queue nor shutdown
                        # fired in 15s. Check for client-side disconnect
                        # (transport hasn't told us yet, but ASGI exposes
                        # it via ``request.is_disconnected``), then emit
                        # a comment line so proxies keep the socket open.
                        if await request.is_disconnected():
                            break
                        yield b": keepalive\n\n"
                        continue

                    # Queue won the race: pull the Event out of the
                    # completed task. ``result()`` will not block because
                    # the task is in ``done``.
                    event = queue_task.result()

                    # Per-connection error isolation: a raising policy
                    # callable must not affect other connections. We
                    # catch *any* exception from ``apply`` (or the JSON
                    # serialization below), log it, and skip the event
                    # for this client only.
                    try:
                        allowed = host.outbound_policies.apply(event, client_ctx)
                    except Exception:
                        sse_logger.exception(
                            "Outbound policy raised on %s for client %s; "
                            "dropping event for this connection only",
                            type(event).__name__,
                            getattr(client_ctx, "user_id", "<unknown>"),
                        )
                        continue

                    if not allowed:
                        # Policy denied this Event for this client.
                        # AC: ``events_pushed`` must NOT increment here.
                        continue

                    try:
                        payload = dataclasses.asdict(event)
                    except TypeError:
                        # Event isn't a dataclass — shouldn't happen given
                        # ``Event`` itself is a dataclass, but defensive.
                        sse_logger.exception(
                            "Event %s is not a dataclass; dropping",
                            type(event).__name__,
                        )
                        continue

                    frame = _format_sse_frame(type(event).__name__, next(id_counter), payload)
                    # Counter increments at the wire-write boundary, not
                    # at the policy check — denied events do not count.
                    metrics.events_pushed += 1
                    yield frame
            except asyncio.CancelledError:
                # Transport closed the streaming task. Re-raise after
                # ``finally`` has unsubscribed so the framework sees a
                # clean cancellation rather than a swallowed one.
                raise
            finally:
                for event_cls, callback in registrations:
                    host.event_bus.unsubscribe(event_cls, callback)
                coordinator.unregister_connection(loop, shutdown_event)
                sse_logger.debug(
                    "SSE connection closed; unsubscribed %d callback(s)",
                    len(registrations),
                )

        # ``X-Accel-Buffering: no`` disables nginx response buffering when
        # this app runs behind an nginx proxy — without it, nginx would
        # batch SSE frames into chunks and break the live-push UX.
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            event_stream(),
            media_type=_SSE_MEDIA_TYPE,
            headers=headers,
        )

    # Surface metrics + shutdown coordinator on the router so callers
    # (tests, user code wiring shutdown) can reach them without an extra
    # return-tuple. ``APIRouter`` is a plain Python object; setattr is
    # the same idiom ``build_manifest_router`` uses for ``invalidate``.
    router.metrics = metrics  # type: ignore[attr-defined]
    router._sse_shutdown_coordinator = coordinator  # type: ignore[attr-defined]
    return router


def register_with_host(
    host: ModuleHost,
    router: APIRouter,
    *,
    shutdown_grace: float = 5.0,
) -> None:
    """Wire the SSE router's shutdown drain into ``host.shutdown``.

    Args:
        host: The :class:`ModuleHost` whose ``shutdown`` should also
            drain SSE connections.
        router: The router returned by :func:`build_sse_router`. We pull
            the coordinator off the private ``_sse_shutdown_coordinator``
            attribute; if it isn't there the caller passed something
            unrelated and we raise ``TypeError``.
        shutdown_grace: Maximum seconds to wait for in-flight SSE
            connections to drain before letting the rest of
            ``host.shutdown`` continue. Defaults to 5 seconds — short
            enough that integration tests don't stall, long enough that
            a slow client gets a chance to receive its last frame.

    How it works
    ------------

    Mirrors :func:`attach_manifest_cache_invalidator`'s monkey-patch
    pattern: replace ``host.shutdown`` with a wrapper that flips the
    coordinator's flag (so new connections start getting 503s and
    streaming generators wake up via their per-connection
    ``asyncio.Event``), waits up to ``shutdown_grace`` for the active
    count to hit zero, then delegates to the original ``shutdown``.

    The original ``shutdown`` then runs as before: unregisters Modules,
    clears the EventBus, and shuts down the thread-pool executor. By
    the time it touches ``event_bus.clear()``, all SSE generators have
    either drained their ``finally`` blocks (and unsubscribed
    themselves) or the grace timeout fired and ``event_bus.clear()``
    will sweep their callbacks. Either way the AC's "no leaked
    EventBus subscriptions" holds.

    Idempotency
    -----------

    A marker attribute on the host makes a second call a no-op. This
    matters because integration tests may build multiple routers on the
    same host across fixtures; we don't want to chain wrappers.
    """
    coordinator: _ShutdownCoordinator | None = getattr(router, "_sse_shutdown_coordinator", None)
    if coordinator is None:
        raise TypeError("register_with_host requires a router returned by build_sse_router")

    if getattr(host, _SSE_SHUTDOWN_HOOK_ATTR, False):
        return

    original_shutdown = host.shutdown

    def shutdown_with_drain(*args: Any, **kwargs: Any) -> Any:
        # Flip the flag + wake all generators *before* calling the
        # original shutdown. The drain wait runs synchronously on the
        # caller thread; meanwhile the uvicorn loop is processing the
        # ``call_soon_threadsafe(event.set)`` callbacks we queued, so
        # each streaming generator exits and decrements the active
        # count.
        coordinator.signal_shutdown()
        coordinator.wait_for_drain(timeout=shutdown_grace)
        return original_shutdown(*args, **kwargs)

    host.shutdown = shutdown_with_drain  # type: ignore[method-assign]
    setattr(host, _SSE_SHUTDOWN_HOOK_ATTR, True)


__all__ = [
    "DEFAULT_SSE_PATH",
    "SSEMetrics",
    "build_sse_router",
    "register_with_host",
]
