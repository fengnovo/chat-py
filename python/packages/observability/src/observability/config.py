"""Configuration loading for the observability runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

OTEL_ENABLED_ENV = "OTEL_ENABLED"
OTEL_EXPORTER_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool
    service_name: str
    service_version: str
    otlp_endpoint: str | None = None


def load_observability_config(
    env: dict[str, str] | None = None,
    *,
    service_name: str,
    service_version: str,
) -> ObservabilityConfig:
    """Build an ObservabilityConfig from an environment mapping.

    Reads OTEL_ENABLED and OTEL_EXPORTER_OTLP_ENDPOINT. When `env` is None,
    falls back to os.environ.
    """
    source = env if env is not None else dict(os.environ)
    enabled = source.get(OTEL_ENABLED_ENV, "").strip().lower() in _TRUE_VALUES
    endpoint = source.get(OTEL_EXPORTER_OTLP_ENDPOINT_ENV, "").strip() or None
    return ObservabilityConfig(
        enabled=enabled,
        service_name=service_name,
        service_version=service_version,
        otlp_endpoint=endpoint,
    )
