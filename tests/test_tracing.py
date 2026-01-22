"""
Tests for distributed tracing.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Event,
    EventInput,
    EventOutput,
    Module,
    ModuleHost,
    ModuleHostConfig,
    Span,
    TraceContext,
    Tracer,
    extract_trace_context,
    generate_id,
    get_correlation_id,
    get_tracer,
    inject_trace_context,
    module,
    set_tracer,
)


@dataclass
class TracingInput(EventInput):
    value: str = ""


@dataclass
class TracingOutput(EventOutput):
    result: str = ""


class TracingEvent(Event[TracingInput, TracingOutput]):
    name = "test.tracing"


@module(name="TracingModule")
class TracingModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, TracingEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, TracingEvent):
            event.output = TracingOutput(result=f"traced: {event.input.value}")
            event.handled = True


class TestGenerateId:
    """Tests for ID generation."""

    def test_generates_unique_ids(self):
        """IDs should be unique."""
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100

    def test_id_format(self):
        """IDs should be 16 character hex strings."""
        id_ = generate_id()
        assert len(id_) == 16
        assert all(c in "0123456789abcdef" for c in id_)


class TestSpan:
    """Tests for Span class."""

    def test_span_creation(self):
        """Span can be created."""
        span = Span(name="test_operation", trace_id="abc123")

        assert span.name == "test_operation"
        assert span.trace_id == "abc123"
        assert span.status == "ok"
        assert span.end_time is None

    def test_span_end(self):
        """Span can be ended."""
        span = Span(name="test")
        span.end("ok")

        assert span.end_time is not None
        assert span.status == "ok"
        assert span.duration_ms is not None

    def test_span_add_event(self):
        """Span can have events."""
        span = Span(name="test")
        span.add_event("checkpoint", {"data": "value"})

        assert len(span.events) == 1
        assert span.events[0]["name"] == "checkpoint"
        assert span.events[0]["attributes"]["data"] == "value"

    def test_span_attributes(self):
        """Span can have attributes."""
        span = Span(name="test")
        span.set_attribute("key", "value")

        assert span.attributes["key"] == "value"

    def test_span_to_dict(self):
        """Span can be serialized."""
        span = Span(name="test", trace_id="abc")
        span.end()

        data = span.to_dict()

        assert data["name"] == "test"
        assert data["trace_id"] == "abc"
        assert data["status"] == "ok"
        assert data["duration_ms"] is not None


class TestTraceContext:
    """Tests for TraceContext class."""

    def test_context_creation(self):
        """Trace context can be created."""
        ctx = TraceContext()

        assert ctx.trace_id
        assert ctx.correlation_id
        assert len(ctx.spans) == 0

    def test_start_span(self):
        """Context can start spans."""
        ctx = TraceContext()
        span = ctx.start_span("operation")

        assert span.name == "operation"
        assert span.trace_id == ctx.trace_id
        assert ctx.current_span is span

    def test_nested_spans(self):
        """Context supports nested spans."""
        ctx = TraceContext()

        span1 = ctx.start_span("outer")
        span2 = ctx.start_span("inner")

        assert span2.parent_span_id == span1.span_id
        assert ctx.current_span is span2

    def test_end_span(self):
        """Context can end spans."""
        ctx = TraceContext()
        ctx.start_span("operation")
        ended = ctx.end_span()

        assert ended is not None
        assert ended.end_time is not None
        assert ctx.current_span is None

    def test_span_context_manager(self):
        """Context supports span as context manager."""
        ctx = TraceContext()

        with ctx.span("operation") as span:
            assert ctx.current_span is span

        assert ctx.current_span is None
        assert span.end_time is not None

    def test_span_context_manager_error(self):
        """Span context manager handles errors."""
        ctx = TraceContext()

        with pytest.raises(ValueError), ctx.span("operation") as span:
            raise ValueError("Test error")

        assert span.status == "error"
        assert "error" in span.attributes

    def test_to_dict(self):
        """Context can be serialized."""
        ctx = TraceContext()

        with ctx.span("operation"):
            pass

        data = ctx.to_dict()

        assert data["trace_id"] == ctx.trace_id
        assert data["correlation_id"] == ctx.correlation_id
        assert len(data["spans"]) == 1


class TestTracer:
    """Tests for Tracer class."""

    def test_trace_context_manager(self):
        """Tracer creates trace with context manager."""
        tracer = Tracer(service_name="test-service")

        with tracer.trace("operation") as ctx:
            assert ctx is not None
            assert ctx.current_span is not None

        assert ctx.spans[0].end_time is not None

    def test_tracer_stores_traces(self):
        """Tracer stores traces."""
        tracer = Tracer()

        with tracer.trace("op1") as ctx1:
            pass

        with tracer.trace("op2") as ctx2:
            pass

        assert tracer.get_trace(ctx1.trace_id) is ctx1
        assert tracer.get_trace(ctx2.trace_id) is ctx2

    def test_current_trace(self):
        """Tracer tracks current trace."""
        tracer = Tracer()

        with tracer.trace("operation"):
            assert tracer.get_current_trace() is not None
            assert tracer.get_current_span() is not None

        assert tracer.get_current_trace() is None

    def test_correlation_id(self):
        """Tracer supports custom correlation ID."""
        tracer = Tracer()

        with tracer.trace("operation", correlation_id="my-correlation-id") as ctx:
            assert ctx.correlation_id == "my-correlation-id"

    def test_export_func(self):
        """Tracer calls export function."""
        exported = []

        def export(ctx):
            exported.append(ctx)

        tracer = Tracer(export_func=export)

        with tracer.trace("operation"):
            pass

        assert len(exported) == 1

    def test_clear_traces(self):
        """Tracer can clear traces."""
        tracer = Tracer()

        with tracer.trace("op1"):
            pass
        with tracer.trace("op2"):
            pass

        count = tracer.clear_traces()
        assert count == 2


class TestGlobalTracer:
    """Tests for global tracer functions."""

    def test_get_set_tracer(self):
        """Can get and set global tracer."""
        tracer = Tracer(service_name="custom")
        set_tracer(tracer)

        assert get_tracer() is tracer

    def test_get_correlation_id(self):
        """Can get correlation ID from current trace."""
        tracer = Tracer()
        set_tracer(tracer)

        with tracer.trace("operation", correlation_id="test-id"):
            assert get_correlation_id() == "test-id"

        assert get_correlation_id() is None


class TestEventTracing:
    """Tests for event tracing utilities."""

    def test_inject_trace_context(self):
        """Trace context is injected into event."""
        tracer = Tracer()
        set_tracer(tracer)

        event = TracingEvent(input=TracingInput(value="test"))

        with tracer.trace("operation", correlation_id="my-id") as ctx:
            inject_trace_context(event)

        assert event.meta["trace_id"] == ctx.trace_id
        assert event.meta["correlation_id"] == "my-id"

    def test_extract_trace_context(self):
        """Trace context can be extracted from event."""
        event = TracingEvent(input=TracingInput(value="test"))
        event.meta["trace_id"] = "abc123"
        event.meta["correlation_id"] = "xyz789"
        event.meta["parent_span_id"] = "span123"

        trace_id, correlation_id, parent_span_id = extract_trace_context(event)

        assert trace_id == "abc123"
        assert correlation_id == "xyz789"
        assert parent_span_id == "span123"


class TestHostWithTracing:
    """Tests for ModuleHost with tracing."""

    def test_tracing_injects_context(self):
        """ModuleHost injects trace context when enabled."""
        config = ModuleHostConfig(enable_tracing=True)
        host = ModuleHost(config=config)
        host.register(TracingModule())

        event = TracingEvent(input=TracingInput(value="test"))
        host.handle(event)

        # Event should have trace context
        assert "correlation_id" in event.meta

    def test_tracing_disabled_no_context(self):
        """ModuleHost doesn't inject trace context when disabled."""
        config = ModuleHostConfig(enable_tracing=False)
        host = ModuleHost(config=config)
        host.register(TracingModule())

        event = TracingEvent(input=TracingInput(value="test"))
        host.handle(event)

        # Event should not have trace context
        assert "trace_id" not in event.meta
