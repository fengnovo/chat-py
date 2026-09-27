"""Sensitivity policy: detect content that must not be persisted as memory."""

from __future__ import annotations

import re

_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Labeled secrets: "password: ...", "api_key = ...", "token: ...", etc.
    re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]"),
    re.compile(r"(?i)\bapi[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\b(access|refresh|id|auth|session)?[_-]?token\b\s*[:=]"),
    re.compile(r"(?i)\b(client[_-]?)?secret\b\s*[:=]"),
    re.compile(r"(?i)\bcredentials?\b\s*[:=]"),
    # Bearer tokens
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    # Private key blocks
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    # Well-known API key formats
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{16,}\b"),  # Stripe
    re.compile(r"\bgh[pousr]_[0-9a-zA-Z]{36,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[0-9a-zA-Z]{20,}\b"),  # OpenAI-style keys
    re.compile(r"\bxox[baprs]-[0-9a-zA-Z-]{10,}\b"),  # Slack tokens
    # US SSN
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit card numbers (13–19 digits, optionally space/dash grouped)
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)


def is_sensitive_memory(content: str) -> bool:
    """Return True when content matches a known sensitive pattern.

    Covers passwords, API keys, tokens, secrets, credentials, SSNs,
    credit card numbers, and private keys.
    """
    return any(pattern.search(content) for pattern in _SENSITIVE_PATTERNS)
