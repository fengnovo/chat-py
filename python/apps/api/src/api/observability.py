"""API-level observability hooks — mirrors apps/api/src/observability.ts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.semconv.trace import SpanAttributes

logger = logging.getLogger(__name__)

REQUEST_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


def resolve_request_id(value: str | None) -> str:
    import uuid
    if value and len(value) <= 128:
        return value
    return str(uuid.uuid4())


class ApiObservability:
    def __init__(self, runtime: Any, enabled: bool, service_version: str, exporter: str) -> None:
        self.runtime = runtime
        self.enabled = enabled
        self.service_version = service_version
        self.exporter = exporter
        self.health = {"enabled": enabled, "exporter": exporter}
        self._tracer = runtime.tracer if runtime else trace.get_tracer(__name__)
        self._meter = runtime.meter if runtime else metrics.get_meter(__name__)

    def start_sse(self, operation: str) -> "SSETelemetry":
        return SSETelemetry(self._tracer, self._meter, operation)

    def mark_request(self, request: Request, event: str) -> None:
        span = request.state.span if hasattr(request.state, "span") else None
        if span:
            span.add_event(event)


class SSETelemetry:
    def __init__(self, tracer: Any, meter: Any, operation: str) -> None:
        self._tracer = tracer
        self._operation = operation
        self._started_at = time.monotonic()
        self._finished = False
        self._first_byte_recorded = False
        self._span = None
        try:
            self._span = tracer.start_span(f"sse {operation}")
            self._span.set_attribute("sse.operation", operation)
        except Exception:
            pass

    def first_byte(self) -> None:
        if self._first_byte_recorded:
            return
        self._first_byte_recorded = True
        if self._span:
            self._span.add_event("sse.first_byte")

    def finish(self, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        if self._span:
            self._span.set_attribute("sse.disconnect_reason", reason)
            self._span.end()


class EnqueueTelemetry:
    def __init__(self, tracer: Any, request_id: str, job_kind: str) -> None:
        self._tracer = tracer
        self._request_id = request_id
        self._job_kind = job_kind
        self._span = None
        try:
            self._span = tracer.start_span(
                "agent.run.enqueue",
                kind=trace.SpanKind.PRODUCER,
                attributes={
                    "messaging.system": "arq",
                    "messaging.destination.name": "agent-runs",
                    "job.kind": job_kind,
                },
            )
        except Exception:
            pass
        self._parent = trace.set_span_in_context(self._span) if self._span else otel_context.get_current()

    def get_context(self) -> dict[str, str | None]:
        from observability import inject_observability_context
        return inject_observability_context(self._parent, self._request_id)

    def finish(self, run_id: str | None = None, outbox_id: str | None = None, created: bool = False, error: Any = None) -> None:
        if not self._span:
            return
        if run_id:
            self._span.set_attribute("run_id", run_id)
        if outbox_id:
            self._span.set_attribute("outbox_id", outbox_id)
        self._span.set_attribute("enqueue.created", created)
        if error:
            self._span.set_status(trace.Status(trace.StatusCode.ERROR))
            self._span.record_exception(error if isinstance(error, Exception) else Exception(str(error)))
        self._span.end()


def start_run_enqueue(observability: ApiObservability, *, request_id: str, job_kind: str) -> EnqueueTelemetry:
    return EnqueueTelemetry(observability._tracer, request_id, job_kind)


def register_observability_middleware(app: FastAPI, observability: ApiObservability) -> None:
    @app.middleware("http")
    async def observability_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        # Strip internal telemetry headers
        for name in list(request.headers.keys()):
            lower = name.lower()
            if lower.startswith("x-internal-") or lower.startswith("x-telemetry-") or lower.startswith("x-observability-") or lower in ("baggage", "x-trace-id", "x-span-id"):
                del request.scope["headers"][next(
                    i for i, (k, _) in enumerate(request.scope["headers"]) if k.decode() == name
                )]

        request_id = resolve_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id

        # Extract trace context
        parent = otel_context.get_current()
        try:
            traceparent = request.headers.get("traceparent")
            tracestate = request.headers.get("tracestate")
            from observability import extract_observability_context
            parent = extract_observability_context({
                **({"traceparent": traceparent} if traceparent else {}),
                **({"tracestate": tracestate} if tracestate else {}),
                "request_id": request_id,
            })
        except Exception:
            pass

        span = None
        try:
            span = observability._tracer.start_span(
                f"{request.method} request",
                kind=trace.SpanKind.SERVER,
                context=parent,
                attributes={
                    SpanAttributes.HTTP_REQUEST_METHOD: request.method,
                    "request_id": request_id,
                },
            )
        except Exception:
            pass

        request.state.span = span
        started_at = time.monotonic()

        response = await call_next(request)

        duration_ms = (time.monotonic() - started_at) * 1000
        status = response.status_code
        status_class = f"{status // 100}xx"
        outcome = "failure" if status >= 500 else "success"

        if status in (401, 403):
            observability.mark_request(request, "auth.failure")
        if status == 429:
            observability.mark_request(request, "rate_limit.rejected")
        if status >= 500:
            observability.mark_request(request, "http.error")

        if span:
            span.set_attribute(SpanAttributes.HTTP_ROUTE, request.url.path)
            span.set_attribute(SpanAttributes.HTTP_RESPONSE_STATUS_CODE, status)
            span.set_status(trace.Status(
                trace.StatusCode.ERROR if status >= 500 else trace.StatusCode.OK
            ))
            span.end()

        response.headers["x-request-id"] = request_id
        return response
