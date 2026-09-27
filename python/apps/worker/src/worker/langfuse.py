"""Worker 专用 Langfuse 适配 — 镜像 apps/worker/src/langfuse.ts。

- 只有 Worker 会创建 GenAI callback；API / Knowledge Service 永远不建。
- 采样按 run 粒度决策，命中才创建 CallbackHandler（一次 run 最多一个）。
- traceMetadata 是固定 allow-list，绝不包含 prompt/completion/tool args/userId 原值；
  正文由 LangfuseSpanProcessor 的 mask 在导出口二次拦截。
- 所有调用 fail-open：handler 构造失败 = 本次 run 不写 Langfuse，业务不受影响。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langfuse.langchain import CallbackHandler


# 简单采样与伪名函数（镜像 @repo/observability 中的逻辑）
def pseudonymize_user_id(user_id: str, secret: str) -> str:
    """基于 secret 与 user_id 的不可逆伪名。"""
    return hashlib.sha256(f"{secret}:{user_id}".encode()).hexdigest()[:16]


def short_run_id(run_id: str) -> str:
    """取 run_id 前 8 位作为短引用。"""
    return run_id[:8]


def sample_run(rate: float) -> bool:
    """采样率 0.0–1.0。"""
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    import random

    return random.random() < rate


LangfuseRunMeta = dict[str, Any]


class WorkerLangfuse:
    """Worker 侧 Langfuse 封装。"""

    def __init__(
        self,
        *,
        enabled: bool,
        secret_key: str | None,
        environment: str,
        shutdown_timeout_ms: int,
        sample_rate: float = 0.0,
        base_url: str | None = None,
        public_key: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._secret_key = secret_key
        self._environment = environment
        self._shutdown_timeout_ms = shutdown_timeout_ms
        self._sample_rate = sample_rate
        self._base_url = base_url
        self._public_key = public_key

    @property
    def enabled(self) -> bool:
        return self._enabled

    def run_callbacks(self, meta: LangfuseRunMeta) -> list[Any]:
        """采样命中时返回恰好一个 LangChain callback；未命中或任何异常返回空列表。"""
        if not self._enabled:
            return []
        try:
            if not sample_run(self._sample_rate):
                return []
            if not self._secret_key:
                return []
            tags = self._build_tags(meta)
            trace_metadata = self._build_trace_metadata(meta)
            handler = CallbackHandler(
                secret_key=self._secret_key,
                public_key=self._public_key or "",
                host=self._base_url,
                user_id=pseudonymize_user_id(meta["user_id"], self._secret_key),
                session_id=meta["session_id"],
                tags=tags,
                trace_metadata=trace_metadata,
            )
            return [handler] if handler else []
        except Exception:
            return []

    def _build_tags(self, meta: LangfuseRunMeta) -> list[str]:
        env_tag = self._bounded_label(self._environment)
        kind_tag = {
            "start": "run:start",
            "resume-approval": "run:resume-approval",
            "resume-question": "run:resume-question",
        }.get(meta.get("run_kind"))
        family_tag = self._bounded_label(meta.get("model_family"))
        tags = [t for t in [env_tag, kind_tag, family_tag] if t]
        return list(set(tags))

    def _build_trace_metadata(self, meta: LangfuseRunMeta) -> dict[str, str]:
        result: dict[str, str] = {
            "run_id": short_run_id(meta["run_id"]),
            "run_kind": meta.get("run_kind", "start"),
            "environment": self._bounded_label(self._environment) or "development",
        }
        provider = self._bounded_label(meta.get("provider"))
        if provider:
            result["provider"] = provider
        model = self._bounded_label(meta.get("model"))
        if model:
            result["model"] = model
        family = self._bounded_label(meta.get("model_family"))
        if family:
            result["model_family"] = family
        return result

    @staticmethod
    def _bounded_label(value: str | None) -> str | None:
        """标签/metadata 只允许稳定短值，超长或含异常字符直接丢弃，避免用户内容借字段混入。"""
        if not value:
            return None
        trimmed = value.strip()
        if re.match(r"^[A-Za-z0-9_.:-]{1,40}$", trimmed):
            return trimmed
        return None

    async def flush(self, timeout_ms: int | None = None) -> None:
        if not self._enabled:
            return
        try:
            # Langfuse SDK 的 flush 是同步的，使用 asyncio.to_thread 包装。
            import asyncio

            await asyncio.to_thread(self._flush_sync, timeout_ms)
        except Exception:
            pass

    def _flush_sync(self, timeout_ms: int | None = None) -> None:
        # CallbackHandler 通常不提供顶层 flush，这里留空作为兼容层。
        pass

    async def shutdown(self, timeout_ms: int | None = None) -> None:
        await self.flush(timeout_ms)


def create_worker_langfuse(
    *,
    shutdown_timeout_ms: int,
    env: dict[str, str] | None = None,
) -> WorkerLangfuse:
    """从环境变量创建 WorkerLangfuse。"""
    source = env if env is not None else {}
    secret_key = (source.get("LANGFUSE_SECRET_KEY") or "").strip() or None
    public_key = (source.get("LANGFUSE_PUBLIC_KEY") or "").strip() or None
    base_url = (source.get("LANGFUSE_BASE_URL") or "").strip() or None
    enabled = bool(secret_key)
    try:
        sample_rate = float(source.get("LANGFUSE_SAMPLE_RATE", "0.0"))
    except ValueError:
        sample_rate = 0.0
    environment = (
        source.get("OTEL_ENVIRONMENT", "").strip()
        or source.get("NODE_ENV", "").strip()
        or "development"
    )
    return WorkerLangfuse(
        enabled=enabled,
        secret_key=secret_key,
        environment=environment,
        shutdown_timeout_ms=shutdown_timeout_ms,
        sample_rate=sample_rate,
        base_url=base_url,
        public_key=public_key,
    )
