"""Rate limiting — mirrors apps/api/src/rate-limit.ts."""

from __future__ import annotations

import re
from dataclasses import dataclass

KNOWLEDGE_UPLOAD_PATH = re.compile(
    r"^/api/knowledge-bases/[^/]+/(?:documents/uploads|documents/[^/]+/confirm|assets/[^/]+/confirm|documents|assets)$"
)


@dataclass(frozen=True)
class RateLimitScope:
    key: str
    limit: int


def is_knowledge_upload_path(pathname: str) -> bool:
    return bool(KNOWLEDGE_UPLOAD_PATH.match(pathname))


def resolve_rate_limit(
    pathname: str,
    tenant_id: str,
    user_id: str,
    rate_limit_requests: int,
    knowledge_upload_rate_limit_requests: int,
) -> RateLimitScope:
    if is_knowledge_upload_path(pathname):
        return RateLimitScope(
            key=f"rate:knowledge-upload:{tenant_id}:{user_id}",
            limit=knowledge_upload_rate_limit_requests,
        )
    return RateLimitScope(
        key=f"rate:api:{tenant_id}:{user_id}",
        limit=rate_limit_requests,
    )
