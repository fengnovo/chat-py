"""W3C trace-context propagation helpers for queue payloads and HTTP carriers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATOR = TraceContextTextMapPropagator()

REQUEST_ID_KEY = "requestId"


@dataclass(frozen=True)
class ObservabilityContext:
    """Serializable propagation snapshot (mirrors contracts.ObservabilityContext)."""

    traceparent: str | None = None
    tracestate: str | None = None
    request_id: str | None = None


def extract_observability_context(carrier: Mapping[str, Any] | None) -> Context:
    """Extract an OTel Context from a carrier dict containing traceparent/tracestate."""
    return _PROPAGATOR.extract(carrier=dict(carrier) if carrier else {})


def inject_observability_context(ctx: Context | None = None, request_id: str | None = None) -> dict[str, str]:
    """Inject the given (or current) context into a carrier dict.

    Returns a dict with traceparent/tracestate headers, plus requestId when provided.
    """
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier, context=ctx)
    if request_id is not None:
        carrier[REQUEST_ID_KEY] = request_id
    return carrier
