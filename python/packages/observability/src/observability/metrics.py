"""Core application metrics: HTTP server, SSE connections, outbox dispatch."""

from __future__ import annotations

from opentelemetry import metrics


class CoreMetrics:
    """Pre-built instruments shared across services.

    All instruments use low-cardinality attributes only (route patterns,
    not raw URLs; outcome/reason enums, not messages).
    """

    def __init__(self, meter: metrics.Meter) -> None:
        self._http_requests = meter.create_counter(
            "http.server.requests",
            description="HTTP requests handled by the server",
        )
        self._http_duration = meter.create_histogram(
            "http.server.duration",
            unit="ms",
            description="HTTP request duration in milliseconds",
        )
        self._sse_connections = meter.create_up_down_counter(
            "sse.connections",
            description="Active SSE connections",
        )
        self._sse_disconnects = meter.create_counter(
            "sse.disconnects",
            description="SSE disconnections by reason",
        )
        self._outbox_dispatch_counter = meter.create_counter(
            "outbox.dispatch",
            description="Outbox dispatch attempts by outcome",
        )

    def http_server(
        self,
        *,
        method: str,
        route: str,
        status: int,
        outcome: str,
        duration_ms: float,
    ) -> None:
        attributes = {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status,
            "outcome": outcome,
        }
        self._http_requests.add(1, attributes)
        self._http_duration.record(duration_ms, attributes)

    def sse_connection(self, *, operation: str, outcome: str, delta: int) -> None:
        self._sse_connections.add(
            delta,
            {"operation": operation, "outcome": outcome},
        )

    def sse_disconnect(self, *, operation: str, reason: str) -> None:
        self._sse_disconnects.add(
            1,
            {"operation": operation, "reason": reason},
        )

    def outbox_dispatch(self, *, outcome: str) -> None:
        self._outbox_dispatch_counter.add(1, {"outcome": outcome})
