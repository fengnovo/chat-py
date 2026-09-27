"""Run Token 校验 — 对应 TS 版 src/run-token.ts。

HS256 + audience="knowledge-service"，claims 结构与 contracts 保持一致。
"""

from __future__ import annotations

from jose import JWTError, jwt

from contracts import KnowledgeRunTokenClaims


async def verify_run_token(header: str | None, secret: str) -> KnowledgeRunTokenClaims:
    try:
        if not header:
            raise PermissionError("missing bearer token")
        raw = header[7:] if header.startswith("Bearer ") else header
        payload = jwt.decode(
            raw,
            secret,
            algorithms=["HS256"],
            audience="knowledge-service",
        )
        return KnowledgeRunTokenClaims.model_validate(payload)
    except JWTError as error:
        raise PermissionError(f"401 Unauthorized: {error}") from error
    except PermissionError:
        raise
    except Exception as error:
        raise PermissionError(f"401 Unauthorized: {error}") from error
