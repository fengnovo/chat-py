"""OpenTelemetry SDK bootstrap: tracer/meter providers and OTLP HTTP exporters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import ObservabilityConfig
from .logger import create_observability_logger


def _signal_endpoint(base: str, signal: str) -> str:
    """Derive a per-signal OTLP HTTP endpoint from a base endpoint.

    Per the OTel spec, OTEL_EXPORTER_OTLP_ENDPOINT is a base URL and
    per-signal paths (/v1/traces, /v1/metrics) are appended.
    """
    trimmed = base.rstrip("/")
    if trimmed.endswith(f"/v1/{signal}"):
        return trimmed
    return f"{trimmed}/v1/{signal}"


@dataclass
class ObservabilityRuntime:
    tracer: trace.Tracer
    meter: metrics.Meter
    logger: Any  # structlog logger
    _tracer_provider: TracerProvider | None = field(default=None, repr=False)
    _meter_provider: MeterProvider | None = field(default=None, repr=False)

    async def shutdown(self) -> None:
        """Flush and shut down providers without blocking the event loop."""
        await asyncio.to_thread(self._shutdown_sync)

    def _shutdown_sync(self) -> None:
        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()
        if self._meter_provider is not None:
            self._meter_provider.shutdown()


async def start_observability(config: ObservabilityConfig) -> ObservabilityRuntime:
    """Initialize OTel providers and return the observability runtime.

    When disabled, returns no-op tracer/meter from the global (unset) API.
    """
    logger = create_observability_logger(config.service_name)

    if not config.enabled:
        return ObservabilityRuntime(
            tracer=trace.get_tracer(config.service_name, config.service_version),
            meter=metrics.get_meter(config.service_name, config.service_version),
            logger=logger,
        )

    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": config.service_version,
        }
    )

    trace_kwargs: dict[str, str] = {}
    metric_kwargs: dict[str, str] = {}
    if config.otlp_endpoint:
        trace_kwargs["endpoint"] = _signal_endpoint(config.otlp_endpoint, "traces")
        metric_kwargs["endpoint"] = _signal_endpoint(config.otlp_endpoint, "metrics")

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**trace_kwargs)))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(**metric_kwargs))],
    )
    metrics.set_meter_provider(meter_provider)

    return ObservabilityRuntime(
        tracer=tracer_provider.get_tracer(config.service_name, config.service_version),
        meter=meter_provider.get_meter(config.service_name, config.service_version),
        logger=logger,
        _tracer_provider=tracer_provider,
        _meter_provider=meter_provider,
    )
