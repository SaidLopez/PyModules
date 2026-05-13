"""
Distributed tracing support for PyModules framework.

Provides correlation IDs, span tracking, and observability middleware.
The OpenTelemetry exporter is in ``pymodules.contrib.tracing.opentelemetry``;
core tracing never imports OTel.
"""

import contextvars
import os
import threading
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .exceptions import UnknownCommandError
from .logging import get_logger
from .middleware import NextCall

if TYPE_CHECKING:
    from .interfaces import Command
    from .middleware import Middleware

tracing_logger = get_logger("tracing")

# Context variable for current trace context
_current_trace: contextvars.ContextVar["TraceContext | None"] = contextvars.ContextVar(
    "current_trace", default=None
)


def generate_id() -> str:
    """Generate a unique trace/span ID."""
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    """A span represents a unit of work in a trace."""

    span_id: str = field(default_factory=generate_id)
    name: str = ""
    trace_id: str = ""
    parent_span_id: str | None = None
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    events: list[dict[str, Any]] = field(default_factory=list)

    def end(self, status: str = "ok") -> None:
        self.end_time = time.time()
        self.status = status

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(
            {
                "name": name,
                "timestamp": time.time(),
                "attributes": attributes or {},
            }
        )

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class TraceContext:
    """
    Context for a distributed trace.

    Holds the trace ID, correlation ID, and current span stack.
    """

    trace_id: str = field(default_factory=generate_id)
    correlation_id: str = field(default_factory=generate_id)
    spans: list[Span] = field(default_factory=list)
    _span_stack: list[Span] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def current_span(self) -> Span | None:
        return self._span_stack[-1] if self._span_stack else None

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        parent_span = self.current_span
        span = Span(
            name=name,
            trace_id=self.trace_id,
            parent_span_id=parent_span.span_id if parent_span else None,
            attributes=attributes or {},
        )
        self._span_stack.append(span)
        self.spans.append(span)
        return span

    def end_span(self, status: str = "ok") -> Span | None:
        if self._span_stack:
            span = self._span_stack.pop()
            span.end(status)
            return span
        return None

    @contextmanager
    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Generator[Span, None, None]:
        span = self.start_span(name, attributes)
        try:
            yield span
        except Exception as e:
            span.set_attribute("error", str(e))
            span.set_attribute("error_type", type(e).__name__)
            self.end_span("error")
            raise
        else:
            self.end_span("ok")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "attributes": self.attributes,
            "spans": [s.to_dict() for s in self.spans],
        }


class Tracer:
    """Tracer for creating and managing traces."""

    def __init__(
        self,
        service_name: str = "pymodules",
        export_func: Callable[[TraceContext], None] | None = None,
    ):
        self.service_name = service_name
        self._export_func = export_func
        self._traces: dict[str, TraceContext] = {}
        self._lock = threading.Lock()

    @contextmanager
    def trace(
        self,
        name: str,
        correlation_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[TraceContext, None, None]:
        ctx = TraceContext(
            correlation_id=correlation_id or generate_id(),
            attributes={
                "service": self.service_name,
                **(attributes or {}),
            },
        )

        with self._lock:
            self._traces[ctx.trace_id] = ctx

        token = _current_trace.set(ctx)

        try:
            with ctx.span(name):
                yield ctx
        finally:
            _current_trace.reset(token)

            if self._export_func:
                try:
                    self._export_func(ctx)
                except Exception as e:
                    tracing_logger.error("Failed to export trace: %s", e)

    def get_trace(self, trace_id: str) -> TraceContext | None:
        with self._lock:
            return self._traces.get(trace_id)

    def get_current_trace(self) -> TraceContext | None:
        return _current_trace.get()

    def get_current_span(self) -> Span | None:
        ctx = self.get_current_trace()
        return ctx.current_span if ctx else None

    def clear_traces(self, older_than: float | None = None) -> int:
        with self._lock:
            if older_than is None:
                count = len(self._traces)
                self._traces.clear()
                return count

            cutoff = time.time() - older_than
            to_remove = [
                tid
                for tid, ctx in self._traces.items()
                if ctx.spans and ctx.spans[0].start_time < cutoff
            ]
            for tid in to_remove:
                del self._traces[tid]
            return len(to_remove)


# Global default tracer
_default_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """Get the default tracer instance."""
    global _default_tracer
    if _default_tracer is None:
        _default_tracer = Tracer()
    return _default_tracer


def set_tracer(tracer: Tracer) -> None:
    """Set the default tracer instance."""
    global _default_tracer
    _default_tracer = tracer


def get_current_trace() -> TraceContext | None:
    """Get the current trace context."""
    return _current_trace.get()


def get_correlation_id() -> str | None:
    """Get the current correlation ID."""
    ctx = get_current_trace()
    return ctx.correlation_id if ctx else None


# =============================================================================
# Command Tracing Utilities
# =============================================================================


def inject_trace_context(command: "Command[Any, Any]") -> None:
    """Inject the current trace context into ``command.context``."""
    ctx = get_current_trace()
    if ctx:
        command.context.trace_id = ctx.trace_id
        command.context.correlation_id = ctx.correlation_id
        if ctx.current_span:
            command.context.parent_span_id = ctx.current_span.span_id
    else:
        if command.context.correlation_id is None:
            command.context.correlation_id = generate_id()


def extract_trace_context(
    command: "Command[Any, Any]",
) -> tuple[str | None, str | None, str | None]:
    """Pull ``(trace_id, correlation_id, parent_span_id)`` from ``command.context``."""
    return (
        command.context.trace_id,
        command.context.correlation_id,
        command.context.parent_span_id,
    )


# =============================================================================
# Observability Middleware
# =============================================================================


class TracingMiddleware:
    """
    Inject the current trace context into the command before dispatching.

    Stateless apart from a counter exposed for observability.
    """

    def __init__(self) -> None:
        self.injected_count = 0

    async def __call__(self, command: "Command[Any, Any]", next_call: NextCall) -> Any:
        inject_trace_context(command)
        self.injected_count += 1
        return await next_call(command)


class MetricsMiddleware:
    """
    Counts dispatched / succeeded / failed / unmatched commands.

    Owns its counters directly; user code holds a reference to read them.
    Unmatched dispatches surface as ``UnknownCommandError`` from the terminal;
    this middleware catches and re-raises it so the count is recorded without
    consuming the signal.

    Attributes:
        dispatched: Total commands seen by the middleware.
        succeeded: Commands that returned without raising and were handled.
        failed: Commands whose inner chain raised (excluding unmatched).
        unmatched: Dispatches where no module claimed the Command class.
    """

    def __init__(self) -> None:
        self.dispatched = 0
        self.succeeded = 0
        self.failed = 0
        self.unmatched = 0

    async def __call__(self, command: "Command[Any, Any]", next_call: NextCall) -> Any:
        self.dispatched += 1
        try:
            result = await next_call(command)
        except UnknownCommandError:
            self.unmatched += 1
            raise
        except Exception:
            self.failed += 1
            raise

        self.succeeded += 1
        return result


class LifecycleMiddleware:
    """
    Run optional lifecycle callbacks around dispatch.

    Replaces the three ``on_event_start``/``on_event_end``/``on_error``
    fields that used to live on ``ModuleHostConfig``.

    Args:
        on_start: Called with the command before the inner chain runs.
        on_end: Called with ``(command, was_handled)`` after success.
        on_error: Called with ``(error, command)`` on failure (before
            the exception propagates).
    """

    def __init__(
        self,
        *,
        on_start: Callable[["Command[Any, Any]"], None] | None = None,
        on_end: Callable[["Command[Any, Any]", bool], None] | None = None,
        on_error: Callable[[Exception, "Command[Any, Any]"], None] | None = None,
    ) -> None:
        self.on_start = on_start
        self.on_end = on_end
        self.on_error = on_error

    async def __call__(self, command: "Command[Any, Any]", next_call: NextCall) -> Any:
        if self.on_start is not None:
            try:
                self.on_start(command)
            except Exception as e:
                tracing_logger.warning("on_start callback failed: %s", e)

        try:
            result = await next_call(command)
        except UnknownCommandError as e:
            # Unmatched dispatch: not a handler error — call on_end with
            # was_handled=False, but skip on_error since no Module ran.
            if self.on_end is not None:
                try:
                    self.on_end(command, False)
                except Exception as cb_e:
                    tracing_logger.warning("on_end callback failed: %s", cb_e)
            raise
        except Exception as e:
            if self.on_error is not None:
                try:
                    self.on_error(e, command)
                except Exception as cb_e:
                    tracing_logger.warning("on_error callback failed: %s", cb_e)
            if self.on_end is not None:
                try:
                    self.on_end(command, False)
                except Exception as cb_e:
                    tracing_logger.warning("on_end callback failed: %s", cb_e)
            raise

        if self.on_end is not None:
            try:
                self.on_end(command, True)
            except Exception as cb_e:
                tracing_logger.warning("on_end callback failed: %s", cb_e)
        return result


def middleware_from_env() -> list["Middleware"]:
    """
    Build tracing/metrics middleware from environment variables:

      - ``PYMODULES_ENABLE_TRACING=true``  → ``TracingMiddleware``
      - ``PYMODULES_ENABLE_METRICS=true``  → ``MetricsMiddleware``
    """
    chain: list[Middleware] = []
    if os.getenv("PYMODULES_ENABLE_TRACING", "false").lower() == "true":
        chain.append(TracingMiddleware())
    if os.getenv("PYMODULES_ENABLE_METRICS", "false").lower() == "true":
        chain.append(MetricsMiddleware())
    return chain


__all__ = [
    "LifecycleMiddleware",
    "MetricsMiddleware",
    "Span",
    "TraceContext",
    "Tracer",
    "TracingMiddleware",
    "extract_trace_context",
    "generate_id",
    "get_correlation_id",
    "get_current_trace",
    "get_tracer",
    "inject_trace_context",
    "middleware_from_env",
    "set_tracer",
]
