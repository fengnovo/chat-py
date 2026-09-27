"""Telemetry hygiene: redact sensitive values and normalize URL cardinality."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "session_key",
    "cookie",
    "ssn",
    "credit_card",
)

_MAX_STRING_LENGTH = 1024
_MAX_DEPTH = 8


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_").replace(".", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_telemetry_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive fields from a value before telemetry export.

    - dict keys containing sensitive fragments (password, token, secret, ...)
      have their values replaced with "[REDACTED]"
    - long strings are truncated to bound attribute size
    - other values pass through unchanged
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_key(key) else redact_telemetry_value(val, _depth=_depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_telemetry_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        return value[:_MAX_STRING_LENGTH] + "…"
    return value


def normalize_route(url: str, route_pattern: str | None = None) -> str:
    """Normalize a request URL to a low-cardinality route label.

    Prefers the framework-provided route pattern; otherwise strips query
    string and fragment from the URL.
    """
    if route_pattern:
        return route_pattern
    parts = urlsplit(url)
    return parts.path or "/"
