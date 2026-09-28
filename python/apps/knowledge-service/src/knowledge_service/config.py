"""knowledge-service 配置 — 对应 TS 版 src/config.ts 的 zod schema。

环境变量映射与 TS 完全一致；caption 相关布尔严格按 "true"/"false" 字符串解析，
绝不使用 truthy 强转（"false" 字符串在 truthy 语义下会误开启 caption）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingProvider = Literal["openai", "bailian", "openai-compatible"]

_TRUE = "true"
_FALSE = "false"


def _default_embedding_base_url(provider: EmbeddingProvider) -> str | None:
    """按 provider 给出默认 embedding base URL。"""
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "bailian":
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return None


def _normalize_extraction_model(value: str | None) -> str:
    """EXTRACTION_MODEL 允许 "openai:gpt-4o-mini" 这种带 provider 前缀的写法，实际调用只用模型名。"""
    return re.sub(r"^[a-z0-9-]+:", "", (value or "gpt-4o-mini").strip(), flags=re.IGNORECASE)


class KnowledgeServiceConfig(BaseSettings):
    """环境变量驱动的服务配置（字段别名 = TS 版读取的环境变量名）。"""

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    # ── 服务监听 ────────────────────────────────────────────────────────
    host: str = Field(default="127.0.0.1", alias="KNOWLEDGE_SERVICE_HOST")
    port: int = Field(default=8091, gt=0, alias="KNOWLEDGE_SERVICE_PORT")

    # ── 基础设施 ────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://127.0.0.1:6379", alias="REDIS_URL")
    postgres_url: str = Field(min_length=1, alias="DATABASE_URL")
    token_secret: str = Field(min_length=16, alias="GRAPHRAG_TOKEN_SECRET")
    qdrant_url: str = Field(min_length=1, alias="QDRANT_URL")
    qdrant_collection_prefix: str = Field(default="knowledge", min_length=1, alias="QDRANT_COLLECTION_PREFIX")

    # ── Embedding ───────────────────────────────────────────────────────
    embedding_provider: EmbeddingProvider = Field(default="openai", alias="EMBEDDING_PROVIDER")
    embedding_model: str = Field(min_length=1, alias="EMBEDDING_MODEL")
    embedding_api_key: str = Field(min_length=1, alias="EMBEDDING_API_KEY")
    embedding_base_url: str = Field(min_length=1, alias="EMBEDDING_BASE_URL")
    embedding_dimension: int = Field(gt=0, alias="EMBEDDING_DIM")
    embedding_profile: str = Field(min_length=1, alias="EMBEDDING_PROFILE")

    # ── 图谱抽取 LLM ────────────────────────────────────────────────────
    extraction_model: str = Field(default="gpt-4o-mini", min_length=1, alias="EXTRACTION_MODEL")
    extraction_base_url: str = Field(min_length=1, alias="EXTRACTION_BASE_URL")
    extraction_api_key: str = Field(min_length=1, alias="EXTRACTION_API_KEY")

    # ── S3 / MinIO ──────────────────────────────────────────────────────
    s3_endpoint: str = Field(default="http://127.0.0.1:59000", alias="S3_ENDPOINT")
    s3_public_endpoint: str | None = Field(default=None, alias="S3_PUBLIC_ENDPOINT")
    s3_region: str = Field(default="us-east-1", min_length=1, alias="S3_REGION")
    s3_bucket: str = Field(default="agent-artifacts", min_length=1, alias="S3_BUCKET")
    s3_access_key: str = Field(default="agent", min_length=1, alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="agent-local-secret", min_length=1, alias="S3_SECRET_KEY")

    # ── VLM 图片 caption（异步队列 knowledge-caption）────────────────────
    caption_enabled: bool = Field(default=False, alias="CAPTION_ENABLED")
    caption_provider: Literal["openai-compatible"] = Field(default="openai-compatible", alias="CAPTION_PROVIDER")
    caption_base_url: str | None = Field(default=None, alias="CAPTION_BASE_URL")
    caption_api_key: str | None = Field(default=None, alias="CAPTION_API_KEY")
    caption_model: str = Field(default="qwen-vl-plus", min_length=1, alias="CAPTION_MODEL")
    caption_timeout_ms: int = Field(default=60_000, gt=0, alias="CAPTION_TIMEOUT_MS")
    caption_max_chars: int = Field(default=240, gt=0, alias="CAPTION_MAX_CHARS")
    caption_concurrency: int = Field(default=2, gt=0, alias="CAPTION_CONCURRENCY")
    caption_lease_ms: int = Field(default=180_000, gt=0, alias="CAPTION_LEASE_MS")
    caption_max_attempts: int = Field(default=3, gt=0, alias="CAPTION_MAX_ATTEMPTS")
    # 混合思考模型（qwen3-omni 等）默认开思考，思考 token 计入 max_tokens 会把 caption 挤空；
    # 置 true 时向请求体注入 enable_thinking:false。
    caption_disable_thinking: bool = Field(default=False, alias="CAPTION_DISABLE_THINKING")

    # ── 索引消费者 ──────────────────────────────────────────────────────
    lease_ms: int = Field(default=120_000, gt=0, alias="KNOWLEDGE_INDEX_LEASE_MS")
    concurrency: int = Field(default=2, gt=0, alias="KNOWLEDGE_CONCURRENCY")
    budget: float = Field(default=1, gt=0, alias="KNOWLEDGE_BUDGET")

    @field_validator("caption_enabled", "caption_disable_thinking", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool:
        # 严格解析 "true"/"false" 字符串；缺省 / 其他值一律按 false 处理。
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == _TRUE
        return False

    @model_validator(mode="before")
    @classmethod
    def _apply_fallbacks(cls, data: Any) -> Any:
        """复刻 TS loadConfig 的回退链：token secret / extraction / embedding base URL。

        mode="before" 在 pydantic-settings 注入环境变量之前执行，
        因此需要从 os.environ 直接读取 fallback 源值。
        """
        if not isinstance(data, dict):
            return data
        merged = dict(data)

        # token secret：GRAPHRAG_TOKEN_SECRET ?? KNOWLEDGE_TOKEN_SECRET
        if not merged.get("GRAPHRAG_TOKEN_SECRET"):
            fallback = merged.get("KNOWLEDGE_TOKEN_SECRET") or os.environ.get("KNOWLEDGE_TOKEN_SECRET")
            if fallback:
                merged["GRAPHRAG_TOKEN_SECRET"] = fallback

        # extraction：EXTRACTION_BASE_URL ?? OPENAI_BASE_URL；EXTRACTION_API_KEY ?? OPENAI_API_KEY
        if not merged.get("EXTRACTION_BASE_URL"):
            fallback = merged.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            if fallback:
                merged["EXTRACTION_BASE_URL"] = fallback
        if not merged.get("EXTRACTION_API_KEY"):
            fallback = merged.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if fallback:
                merged["EXTRACTION_API_KEY"] = fallback

        # embedding base URL：未配置时按 provider 取默认值
        if not merged.get("EMBEDDING_BASE_URL"):
            provider = (merged.get("EMBEDDING_PROVIDER") or os.environ.get("EMBEDDING_PROVIDER") or "openai").strip()
            default_url = _default_embedding_base_url(provider)  # type: ignore[arg-type]
            if default_url:
                merged["EMBEDDING_BASE_URL"] = default_url

        # extraction model：剥离 provider 前缀
        raw_model = merged.get("EXTRACTION_MODEL") or os.environ.get("EXTRACTION_MODEL")
        merged["EXTRACTION_MODEL"] = _normalize_extraction_model(raw_model)
        return merged

    @property
    def caption_ready(self) -> bool:
        """caption 工作器启动条件：开关打开且 baseUrl + apiKey 齐备。"""
        return bool(self.caption_enabled and self.caption_base_url and self.caption_api_key)


def load_config(env: dict[str, str] | None = None) -> KnowledgeServiceConfig:
    """从环境（或显式 dict）加载配置；与 TS loadConfig(env) 语义一致。"""
    if env is not None:
        return KnowledgeServiceConfig(**{k: v for k, v in env.items() if v is not None})
    return KnowledgeServiceConfig()
