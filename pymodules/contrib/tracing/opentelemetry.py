"""
OpenTelemetry exporter for PyModules traces.

Lives in contrib because it imports the ``opentelemetry`` package on
construction. Core ``pymodules.tracing`` knows nothing about OTel.
"""

from typing import TYPE_CHECKING

from ...logging import get_logger

if TYPE_CHECKING:
    from ...tracing import TraceContext

tracing_logger = get_logger("contrib.tracing.opentelemetry")


class OpenTelemetryExporter:
    """
    Converts PyModules traces to OpenTelemetry format.

    Requires ``opentelemetry-api`` and ``opentelemetry-sdk``.

    Example:
        from pymodules.tracing import Tracer
        from pymodules.contrib.tracing.opentelemetry import OpenTelemetryExporter

        exporter = OpenTelemetryExporter()
        tracer = Tracer(export_func=exporter.export)
    """

    def __init__(self) -> None:
        self._otel_tracer = None
        self._available = False

        try:
            from opentelemetry import trace as otel_trace

            self._otel_trace = otel_trace
            self._otel_tracer = otel_trace.get_tracer("pymodules")
            self._available = True
        except ImportError:
            tracing_logger.debug(
                "OpenTelemetry not available. Install with: "
                "pip install opentelemetry-api opentelemetry-sdk"
            )

    @property
    def available(self) -> bool:
        """True if the ``opentelemetry`` packages imported successfully."""
        return self._available

    def export(self, ctx: "TraceContext") -> None:
        """Push a completed ``TraceContext`` into the OpenTelemetry SDK."""
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


__all__ = ["OpenTelemetryExporter"]
