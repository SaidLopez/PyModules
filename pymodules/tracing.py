"""
Distributed tracing support for PyModules framework.

Provides correlation IDs, span tracking, and optional OpenTelemetry integration.
"""

import contextvars
import threading
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .interfaces import Event

from .logging import get_logger

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
    """
    A span represents a unit of work in a trace.

    Attributes:
        span_id: Unique identifier for this span.
        name: Name of the operation.
        trace_id: ID of the parent trace.
        parent_span_id: ID of the parent span (if any).
        start_time: When the span started.
        end_time: When the span ended.
        attributes: Additional attributes/tags.
        status: Status of the span (ok, error).
        events: List of events that occurred during the span.
    """

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
        """End the span."""
        self.end_time = time.time()
        self.status = status

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """Add an event to the span."""
        self.events.append(
            {
                "name": name,
                "timestamp": time.time(),
                "attributes": attributes or {},
            }
        )

    def set_attribute(self, key: str, value: Any) -> None:
        """Set an attribute on the span."""
        self.attributes[key] = value

    @property
    def duration_ms(self) -> float | None:
        """Get span duration in milliseconds."""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000

    def to_dict(self) -> dict[str, Any]:
        """Convert span to dictionary for serialization."""
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
        """Get the current active span."""
        return self._span_stack[-1] if self._span_stack else None

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """Start a new span."""
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
        """End the current span."""
        if self._span_stack:
            span = self._span_stack.pop()
            span.end(status)
            return span
        return None

    @contextmanager
    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Generator[Span, None, None]:
        """Context manager for creating a span."""
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
        """Convert trace to dictionary for serialization."""
        return {
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "attributes": self.attributes,
            "spans": [s.to_dict() for s in self.spans],
        }


class Tracer:
    """
    Tracer for creating and managing traces.

    Example:
        tracer = Tracer()

        with tracer.trace("process_order") as ctx:
            with ctx.span("validate_order"):
                validate(order)
            with ctx.span("save_order"):
                save(order)

        # Access trace data
        print(tracer.get_trace(ctx.trace_id))
    """

    def __init__(
        self,
        service_name: str = "pymodules",
        export_func: Callable[[TraceContext], None] | None = None,
    ):
        """
        Initialize tracer.

        Args:
            service_name: Name of the service for tracing.
            export_func: Optional function to export completed traces.
        """
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
        """
        Create a new trace context.

        Args:
            name: Name of the trace/root span.
            correlation_id: Optional correlation ID (generated if not provided).
            attributes: Optional attributes for the trace.
        """
        ctx = TraceContext(
            correlation_id=correlation_id or generate_id(),
            attributes={
                "service": self.service_name,
                **(attributes or {}),
            },
        )

        # Store trace
        with self._lock:
            self._traces[ctx.trace_id] = ctx

        # Set as current trace
        token = _current_trace.set(ctx)

        try:
            with ctx.span(name):
                yield ctx
        finally:
            _current_trace.reset(token)

            # Export trace if configured
            if self._export_func:
                try:
                    self._export_func(ctx)
                except Exception as e:
                    tracing_logger.error("Failed to export trace: %s", e)

    def get_trace(self, trace_id: str) -> TraceContext | None:
        """Get a trace by ID."""
        with self._lock:
            return self._traces.get(trace_id)

    def get_current_trace(self) -> TraceContext | None:
        """Get the current trace context."""
        return _current_trace.get()

    def get_current_span(self) -> Span | None:
        """Get the current span."""
        ctx = self.get_current_trace()
        return ctx.current_span if ctx else None

    def clear_traces(self, older_than: float | None = None) -> int:
        """
        Clear stored traces.

        Args:
            older_than: If provided, only clear traces older than this many seconds.

        Returns:
            Number of traces cleared.
        """
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
# OpenTelemetry Integration (Optional)
# =============================================================================


class OpenTelemetryExporter:
    """
    OpenTelemetry exporter for PyModules traces.

    Converts PyModules traces to OpenTelemetry format.
    Requires opentelemetry-api and opentelemetry-sdk packages.

    Example:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider

        otel_trace.set_tracer_provider(TracerProvider())
        exporter = OpenTelemetryExporter()

        tracer = Tracer(export_func=exporter.export)
    """

    def __init__(self) -> None:
        """Initialize OpenTelemetry exporter."""
        self._otel_tracer = None
        self._available = False

        try:
            from opentelemetry import trace as otel_trace

            self._otel_trace = otel_trace
            self._otel_tracer = otel_trace.get_tracer("pymodules")
            self._available = True
        except ImportError:
            tracing_logger.debug(
                "OpenTelemetry not available. Install with: pip install opentelemetry-api opentelemetry-sdk"
            )

    @property
    def available(self) -> bool:
        """Check if OpenTelemetry is available."""
        return self._available

    def export(self, ctx: TraceContext) -> None:
        """Export a trace context to OpenTelemetry."""
        if not self._available or not self._otel_tracer:
            return

        from opentelemetry.trace import StatusCode

        for span in ctx.spans:
            with self._otel_tracer.start_as_current_span(
                span.name,
                attributes=span.attributes,
            ) as otel_span:
                for event in span.events:
                    otel_span.add_event(event["name"], event.get("attributes", {}))

                if span.status == "error":
                    otel_span.set_status(StatusCode.ERROR)
                else:
                    otel_span.set_status(StatusCode.OK)


# =============================================================================
# Event Tracing Utilities
# =============================================================================


def inject_trace_context(event: "Event") -> None:
    """Inject current trace context into event metadata."""
    ctx = get_current_trace()
    if ctx:
        event.meta["trace_id"] = ctx.trace_id
        event.meta["correlation_id"] = ctx.correlation_id
        if ctx.current_span:
            event.meta["parent_span_id"] = ctx.current_span.span_id
    else:
        # No active trace, but still inject a correlation ID for request tracking
        if "correlation_id" not in event.meta:
            event.meta["correlation_id"] = generate_id()


def extract_trace_context(event: "Event") -> tuple[str | None, str | None, str | None]:
    """Extract trace context from event metadata."""
    return (
        event.meta.get("trace_id"),
        event.meta.get("correlation_id"),
        event.meta.get("parent_span_id"),
    )
