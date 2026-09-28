"""FastAPI Agent API — mirrors apps/api/src/config.ts."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]

DEFAULT_TRUST_PROXY_CIDRS = ",".join([
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
])


class OAuthProviderConfig:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret


class OAuthConfig:
    def __init__(
        self,
        github: OAuthProviderConfig | None = None,
        google: OAuthProviderConfig | None = None,
        callback_url: str | None = None,
        state_secret: str | None = None,
    ) -> None:
        self.github = github
        self.google = google
        self.callback_url = callback_url
        self.state_secret = state_secret


class KnowledgeMCPConfig:
    def __init__(self, url: str, secret: str, timeout_ms: int) -> None:
        self.url = url
        self.secret = secret
        self.timeout_ms = timeout_ms


class KnowledgeQAModelConfig:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


class KnowledgeEmbeddingProfile:
    def __init__(self, key: str, model: str, dimension: int, collection_prefix: str) -> None:
        self.key = key
        self.model = model
        self.dimension = dimension
        self.collection_prefix = collection_prefix


class ApiConfig(BaseSettings):
    """Environment-based configuration, mirrors the Zod schema in config.ts."""

    model_config = {"env_prefix": "", "case_sensitive": False, "extra": "ignore"}

    # Core
    NODE_ENV: Literal["development", "test", "production"] = "development"
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8003
    API_VERSION: str = "0.1.0"
    TRUST_PROXY_CIDRS: str = DEFAULT_TRUST_PROXY_CIDRS
    WEB_ORIGIN: str = "http://localhost:3000"

    # Database
    DATABASE_URL: str = "postgresql://agent:agent@127.0.0.1:55432/agent"

    # Redis
    REDIS_URL: str = "redis://127.0.0.1:56379"

    # S3 / MinIO
    S3_ENDPOINT: str = "http://127.0.0.1:59000"
    S3_PUBLIC_ENDPOINT: str | None = None
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "agent-artifacts"
    S3_ACCESS_KEY: str = "agent"
    S3_SECRET_KEY: str = "agent-local-secret"

    # Uploads
    ARTIFACT_MAX_BYTES: int = 100_000_000
    PROJECT_UPLOAD_MAX_BYTES: int = 20_000_000
    KNOWLEDGE_DOCUMENT_MAX_BYTES: int = 20_000_000

    # Embedding
    EMBEDDING_PROFILE: str | None = None
    EMBEDDING_MODEL: str | None = None
    EMBEDDING_DIM: int | None = None
    QDRANT_COLLECTION_PREFIX: str = "knowledge"

    # Rate limiting
    RATE_LIMIT_REQUESTS: int = 300
    RATE_LIMIT_WINDOW_MS: int = 60_000
    KNOWLEDGE_UPLOAD_RATE_LIMIT_REQUESTS: int = 3_000

    # Outbox
    OUTBOX_POLL_INTERVAL_MS: int = 500
    OUTBOX_BATCH_SIZE: int = 50
    OUTBOX_LEASE_MS: int = 30_000
    OUTBOX_RECONCILE_INTERVAL_MS: int = 5_000
    OUTBOX_STALE_AFTER_MS: int = 30_000

    # Auth
    AUTH_MODE: Literal["dev", "password", "oidc"] = "dev"
    AUTH_JWT_SECRET: str | None = Field(default=None, min_length=32)
    DEV_TENANT_ID: str = "00000000-0000-4000-8000-000000000001"
    DEV_USER_ID: str = "00000000-0000-4000-8000-000000000001"
    SIGNUP_TENANT_ID: str | None = None
    AUTH_SIGNUP_ENABLED: str | None = None

    # OIDC
    OIDC_ISSUER: str | None = None
    OIDC_AUDIENCE: str | None = None
    OIDC_JWKS_URL: str | None = None

    # OAuth
    OAUTH_GITHUB_CLIENT_ID: str | None = None
    OAUTH_GITHUB_CLIENT_SECRET: str | None = None
    OAUTH_GOOGLE_CLIENT_ID: str | None = None
    OAUTH_GOOGLE_CLIENT_SECRET: str | None = None
    OAUTH_CALLBACK_URL: str | None = None
    OAUTH_STATE_SECRET: str | None = Field(default=None, min_length=32)

    @field_validator("OAUTH_STATE_SECRET", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        """空字符串转为 None，避免 min_length 校验失败。"""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # Workspace / Sandbox
    WORKSPACE_ROOT: str = str(REPOSITORY_ROOT / "data" / "workspaces")
    SANDBOX_RUNTIME: Literal["docker", "e2b-cloud"] = "docker"
    SANDBOX_SESSIONS_ROOT: str | None = None
    DOCKER_SANDBOX_SESSIONS_ROOT: str | None = None

    # Knowledge
    KNOWLEDGE_MCP_URL: str | None = None
    KNOWLEDGE_MCP_SECRET: str | None = Field(default=None, min_length=8)
    KNOWLEDGE_TOKEN_SECRET: str | None = Field(default=None, min_length=8)
    KNOWLEDGE_MCP_TIMEOUT_MS: int = 15_000

    # LLM
    OPENAI_BASE_URL: str | None = None
    OPENAI_API_KEY: str | None = None
    MODEL: str | None = None

    # Caption
    CAPTION_ENABLED: str | None = None

    @field_validator("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL", "OAUTH_CALLBACK_URL", mode="before")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v if v else None

    @model_validator(mode="after")
    def _validate(self) -> ApiConfig:
        if self.NODE_ENV == "production" and self.AUTH_MODE == "dev":
            raise ValueError("AUTH_MODE=dev is forbidden in production")
        if self.AUTH_MODE == "password" and not self.AUTH_JWT_SECRET:
            raise ValueError("AUTH_JWT_SECRET (min 32 chars) is required when AUTH_MODE=password")
        if self.AUTH_MODE == "oidc" and (not self.OIDC_ISSUER or not self.OIDC_AUDIENCE or not self.OIDC_JWKS_URL):
            raise ValueError("OIDC_ISSUER, OIDC_AUDIENCE and OIDC_JWKS_URL are required")
        # Embedding profile must be all-or-nothing
        embedding_inputs = [self.EMBEDDING_PROFILE, self.EMBEDDING_MODEL, self.EMBEDDING_DIM]
        has_complete = all(v is not None for v in embedding_inputs)
        has_partial = any(v is not None for v in embedding_inputs)
        if has_partial and not has_complete:
            raise ValueError("EMBEDDING_PROFILE, EMBEDDING_MODEL and EMBEDDING_DIM must be configured together")
        return self

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def parsed_trust_proxy_cidrs(self) -> list[str]:
        entries = [e.strip() for e in self.TRUST_PROXY_CIDRS.split(",") if e.strip()]
        if not entries:
            raise ValueError("TRUST_PROXY_CIDRS must contain at least one CIDR")
        for entry in entries:
            ipaddress.ip_network(entry, strict=False)
        return entries

    @property
    def database_url(self) -> str:
        if os.environ.get("DATABASE_URL"):
            return os.environ["DATABASE_URL"]
        if self.NODE_ENV == "test":
            return "postgresql://agent:agent@127.0.0.1:55433/agent_test"
        return self.DATABASE_URL

    @property
    def signup_tenant_id(self) -> str:
        return self.SIGNUP_TENANT_ID or self.DEV_TENANT_ID

    @property
    def signup_enabled(self) -> bool:
        if self.AUTH_SIGNUP_ENABLED is not None:
            return self.AUTH_SIGNUP_ENABLED == "true"
        return self.NODE_ENV != "production"

    @property
    def knowledge_embedding_profile(self) -> KnowledgeEmbeddingProfile | None:
        if self.EMBEDDING_PROFILE and self.EMBEDDING_MODEL and self.EMBEDDING_DIM:
            return KnowledgeEmbeddingProfile(
                key=self.EMBEDDING_PROFILE,
                model=self.EMBEDDING_MODEL,
                dimension=self.EMBEDDING_DIM,
                collection_prefix=self.QDRANT_COLLECTION_PREFIX,
            )
        return None

    @property
    def knowledge_mcp(self) -> KnowledgeMCPConfig | None:
        url = self.KNOWLEDGE_MCP_URL
        secret = self.KNOWLEDGE_MCP_SECRET or self.KNOWLEDGE_TOKEN_SECRET
        if not url or not secret:
            return None
        return KnowledgeMCPConfig(url=url, secret=secret, timeout_ms=self.KNOWLEDGE_MCP_TIMEOUT_MS)

    @property
    def knowledge_qa_model(self) -> KnowledgeQAModelConfig | None:
        if self.OPENAI_BASE_URL and self.OPENAI_API_KEY:
            return KnowledgeQAModelConfig(
                base_url=self.OPENAI_BASE_URL,
                api_key=self.OPENAI_API_KEY,
                model=self.MODEL or "deepseek-chat",
            )
        return None

    @property
    def caption_enabled(self) -> bool:
        return self.CAPTION_ENABLED == "true"

    @property
    def sandbox_sessions_root(self) -> str:
        root = self.SANDBOX_SESSIONS_ROOT or self.DOCKER_SANDBOX_SESSIONS_ROOT
        if not root:
            root = str(REPOSITORY_ROOT / "data" / "sandboxes")
        p = Path(root)
        # 相对路径按仓库根解析（与 worker 的 mcp_config_path 基准一致）
        return str(p if p.is_absolute() else (REPOSITORY_ROOT / p).resolve())

    @property
    def oauth(self) -> OAuthConfig:
        github = None
        if self.OAUTH_GITHUB_CLIENT_ID and self.OAUTH_GITHUB_CLIENT_SECRET:
            github = OAuthProviderConfig(self.OAUTH_GITHUB_CLIENT_ID, self.OAUTH_GITHUB_CLIENT_SECRET)
        google = None
        if self.OAUTH_GOOGLE_CLIENT_ID and self.OAUTH_GOOGLE_CLIENT_SECRET:
            google = OAuthProviderConfig(self.OAUTH_GOOGLE_CLIENT_ID, self.OAUTH_GOOGLE_CLIENT_SECRET)
        return OAuthConfig(
            github=github,
            google=google,
            callback_url=self.OAUTH_CALLBACK_URL,
            state_secret=self.OAUTH_STATE_SECRET or self.AUTH_JWT_SECRET,
        )

    @property
    def workspace_root(self) -> str:
        p = Path(self.WORKSPACE_ROOT)
        return str(p if p.is_absolute() else (REPOSITORY_ROOT / p).resolve())


def load_config(env: dict[str, str] | None = None) -> ApiConfig:
    """Load config from environment or provided dict."""
    if env:
        return ApiConfig(**{k: v for k, v in env.items() if v is not None})
    return ApiConfig()
