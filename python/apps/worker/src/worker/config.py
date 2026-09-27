"""Worker 配置 — 镜像 apps/worker/src/config.ts 的 Zod schema。

使用 Pydantic BaseSettings 从环境变量加载，并保留与 TS 版一致的默认值、
派生字段（models、绝对路径解析）和启动期校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录：python/apps/worker/src/worker/config.py → 上溯 6 级到仓库根
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]

# 与 agent-core 对齐的默认 Docker 沙箱工作区路径
DOCKER_SANDBOX_WORKSPACE = "/workspace"


class ModelSpec:
    """LLM 模型规格，镜像 agent-core 的 ModelSpec。"""

    def __init__(
        self,
        *,
        id: str,
        model: str,
        provider: str,
        api_key: str,
        max_tokens: int,
        base_url: str | None = None,
    ) -> None:
        self.id = id
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.base_url = base_url


class WorkerConfig(BaseSettings):
    """环境变量配置，字段名与 TS 版 Zod schema 保持一致（大写环境变量）。"""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # 基础
    NODE_ENV: Literal["development", "test", "production"] = "development"
    DATABASE_URL: str = "postgresql://agent:agent@127.0.0.1:55432/agent"
    REDIS_URL: str = "redis://127.0.0.1:56379"

    # 对象存储
    S3_ENDPOINT: str = "http://127.0.0.1:59000"
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "agent-artifacts"
    S3_ACCESS_KEY: str = "agent"
    S3_SECRET_KEY: str = "agent-local-secret"

    # Worker
    AGENT_DRIVER: Literal["deep"] = "deep"
    WORKER_CONCURRENCY: int = Field(default=2, ge=1, le=32)
    WORKSPACE_ROOT: str = str(REPOSITORY_ROOT / "data" / "workspaces")

    # 沙箱
    SANDBOX_RUNTIME: Literal["docker", "e2b-cloud"] = "docker"
    DOCKER_SANDBOX_IMAGE: str = "chat-agent-sandbox:latest"
    DOCKER_SANDBOX_COMMAND_TIMEOUT_MS: int = Field(default=180_000, ge=1_000, le=600_000)
    DOCKER_SANDBOX_SESSIONS_ROOT: str = str(REPOSITORY_ROOT / "data" / "sandboxes")
    DOCKER_SANDBOX_WORKSPACE_PATH: str = DOCKER_SANDBOX_WORKSPACE
    E2B_API_KEY: str | None = None
    E2B_TEMPLATE: str = "base"
    E2B_TIMEOUT_MS: int = Field(default=3_600_000, ge=60_000, le=86_400_000)
    E2B_WORKSPACE_PATH: str = "/home/user/workspace"
    E2B_API_URL: str | None = None
    E2B_SANDBOX_URL: str | None = None

    # 模型
    MODEL: str = "openai:gpt-4o-mini"
    MODEL_PROVIDER: str = "openai"
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    FALLBACK_MODELS: str | None = None
    MODEL_MAX_TOKENS: int = Field(default=16_000, ge=1_000, le=200_000)
    # 多步编码任务需要数百个 super-step；预算过低会把接近完成的任务判为失败。
    AGENT_RECURSION_LIMIT: int = Field(default=600, ge=50, le=10_000)
    AGENT_MODEL_CALL_LIMIT: int = Field(default=120, ge=10, le=10_000)

    # MCP
    MCP_CONFIG_PATH: str | None = None

    # 知识库 MCP
    KNOWLEDGE_MCP_URL: str | None = None
    KNOWLEDGE_MCP_SECRET: str | None = None
    KNOWLEDGE_MCP_TIMEOUT_MS: int = Field(default=10_000, ge=100, le=120_000)
    KNOWLEDGE_MCP_ENABLED: str = "false"

    # DeepAgents memory/skills：宿主机路径，Worker 启动时上传到沙箱。
    AGENT_MEMORY_FILE: str | None = None
    AGENT_SKILLS_DIR: str | None = None

    # 历史消息压缩阈值
    AGENT_SUMMARIZATION_TRIGGER_TOKENS: int = Field(default=50_000, ge=5_000, le=200_000)
    AGENT_SUMMARIZATION_KEEP_TOKENS: int = Field(default=15_000, ge=1_000, le=100_000)

    # 长期记忆语义索引：复用现有 Qdrant + Embedding；缺任一关键配置则自动关闭索引。
    MEMORY_QDRANT_URL: str | None = None
    MEMORY_QDRANT_API_KEY: str | None = None
    MEMORY_EMBEDDING_URL: str | None = None
    MEMORY_EMBEDDING_API_KEY: str | None = None
    MEMORY_EMBEDDING_MODEL: str | None = None
    MEMORY_EMBEDDING_DIM: int | None = Field(default=None, ge=1, le=8_192)

    # 观测
    OTEL_SERVICE_NAME: str | None = None
    OTEL_ENABLED: str | None = None
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_ENVIRONMENT: str | None = None
    OBSERVABILITY_CAPTURE_CONTENT: str | None = None
    OBSERVABILITY_SHUTDOWN_TIMEOUT_MS: int = 5_000

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_BASE_URL: str | None = None
    LANGFUSE_SAMPLE_RATE: float = 0.0

    @field_validator(
        "E2B_API_URL",
        "E2B_SANDBOX_URL",
        "KNOWLEDGE_MCP_URL",
        "OPENAI_BASE_URL",
        "MEMORY_QDRANT_URL",
        "MEMORY_EMBEDDING_URL",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return v if v else None

    @model_validator(mode="after")
    def _validate(self) -> "WorkerConfig":
        if self.SANDBOX_RUNTIME == "e2b-cloud" and not self.E2B_API_KEY:
            raise ValueError("E2B_API_KEY is required when SANDBOX_RUNTIME=e2b-cloud")
        return self

    # ── 派生属性（镜像 TS loadWorkerConfig 的后处理） ─────────────────────

    @property
    def database_url(self) -> str:
        import os

        if os.environ.get("DATABASE_URL"):
            return os.environ["DATABASE_URL"]
        if self.NODE_ENV == "test":
            return "postgresql://agent:agent@127.0.0.1:55433/agent_test"
        return self.DATABASE_URL

    @property
    def workspace_root(self) -> str:
        return str(Path(self.WORKSPACE_ROOT).resolve())

    @property
    def sandbox_sessions_root(self) -> str:
        return str(Path(self.DOCKER_SANDBOX_SESSIONS_ROOT).resolve())

    @property
    def mcp_config_path(self) -> str | None:
        """相对路径按仓库根解析；绝对路径原样使用。"""
        if not self.MCP_CONFIG_PATH:
            return None
        p = Path(self.MCP_CONFIG_PATH)
        if p.is_absolute():
            return str(p)
        return str((REPOSITORY_ROOT / p).resolve())

    @property
    def knowledge_mcp_enabled(self) -> bool:
        return self.KNOWLEDGE_MCP_ENABLED == "true"

    @property
    def models(self) -> list[ModelSpec]:
        """主模型 + fallback 列表，镜像 TS modelSpec()。缺 API key 时抛错。"""
        raw_models = [self.MODEL]
        if self.FALLBACK_MODELS:
            raw_models.extend(self.FALLBACK_MODELS.split(","))
        specs: list[ModelSpec] = []
        for raw in raw_models:
            item = raw.strip()
            if not item:
                continue
            separator = item.find(":")
            provider = item[:separator] if separator > 0 else self.MODEL_PROVIDER
            model = item[separator + 1 :] if separator > 0 else item
            api_key = (
                self.ANTHROPIC_API_KEY if provider == "anthropic" else self.OPENAI_API_KEY
            )
            if not api_key:
                raise ValueError(f"Missing API key for model provider {provider}")
            specs.append(
                ModelSpec(
                    id=f"{provider}:{model}",
                    model=model,
                    provider=provider,
                    api_key=api_key,
                    max_tokens=self.MODEL_MAX_TOKENS,
                    base_url=(
                        self.OPENAI_BASE_URL
                        if provider == "openai" and self.OPENAI_BASE_URL
                        else None
                    ),
                )
            )
        return specs


def load_worker_config(env: dict[str, str] | None = None) -> WorkerConfig:
    """从环境变量（或显式 dict）加载配置。"""
    if env:
        return WorkerConfig(**{k: v for k, v in env.items() if v is not None})
    return WorkerConfig()
