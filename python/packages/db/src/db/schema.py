"""SQLAlchemy DeclarativeBase models mirroring the Drizzle schema.

Kept as a **single source of truth** for Python-facing table metadata.
The repositories use raw asyncpg SQL, but Alembic auto-generation and
type-checked reflection require these models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    REAL,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {
        dict[str, Any]: JSON,
        list[uuid.UUID]: ARRAY(UUID(as_uuid=True)),
        list[str]: ARRAY(String),
    }


# ─── tenants ──────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── users ────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    external_subject: Mapped[str | None] = mapped_column(String, unique=True)
    username: Mapped[str | None] = mapped_column(String, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── oauth_accounts ───────────────────────────────────────────────────

class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        Index("oauth_accounts_provider_subject_idx", "provider", "subject", unique=True),
        Index("oauth_accounts_user_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="cascade"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── knowledge_base_grants ────────────────────────────────────────────

class KnowledgeBaseGrant(Base):
    __tablename__ = "knowledge_base_grants"
    __table_args__ = (
        Index("knowledge_base_grants_kb_user_idx", "kb_id", "user_id", unique=True),
        Index("knowledge_base_grants_tenant_user_idx", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="cascade"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="cascade"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="cascade"), nullable=False
    )
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="set null")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── tenant_memberships ───────────────────────────────────────────────

class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "user_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── projects ─────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'empty'")
    )
    source_ref: Mapped[str | None] = mapped_column(String)
    source_revision: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── workspaces ───────────────────────────────────────────────────────

class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        Index("workspaces_tenant_path_idx", "tenant_id", "path", unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id")
    )
    path: Mapped[str] = mapped_column(String, nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String)
    sandbox_provider: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'e2b'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── agent_sessions ───────────────────────────────────────────────────

class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        Index("agent_sessions_tenant_updated_idx", "tenant_id", "updated_at"),
        Index("agent_sessions_tenant_external_idx", "tenant_id", "external_key", unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False
    )
    external_key: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    approval_mode: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'manual'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── agent_runs ───────────────────────────────────────────────────────

class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("agent_runs_tenant_idempotency_idx", "tenant_id", "idempotency_key", unique=True),
        Index("agent_runs_tenant_created_idx", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    user_message: Mapped[str] = mapped_column(String, nullable=False)
    continuation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    knowledge_base_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String)
    last_event_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── agent_memories ───────────────────────────────────────────────────

class AgentMemory(Base):
    __tablename__ = "agent_memories"
    __table_args__ = (
        Index(
            "agent_memories_scope_idx",
            "tenant_id",
            "user_id",
            "assistant_key",
            "scope",
            "status",
            "updated_at",
        ),
        Index(
            "agent_memories_active_key_idx",
            "tenant_id",
            "user_id",
            "assistant_key",
            "scope",
            "normalized_key",
            unique=True,
            postgresql_where=sa_text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="cascade"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="cascade"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="cascade")
    )
    assistant_key: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'chat'")
    )
    scope: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    normalized_key: Mapped[str] = mapped_column(String, nullable=False)
    importance: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=sa_text("0.5")
    )
    confidence: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=sa_text("0.5")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'active'")
    )
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="set null")
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="set null")
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_memories.id", ondelete="set null")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, server_default=sa_text("'{}'")
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── memory_jobs ──────────────────────────────────────────────────────

class MemoryJob(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id"),
        Index("memory_jobs_claim_idx", "status", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="cascade"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="cascade"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="cascade"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="cascade"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'queued'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── knowledge_bases ──────────────────────────────────────────────────

class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        Index(
            "knowledge_bases_tenant_status_idx",
            "tenant_id",
            "status",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_bases_tenant_owner_idx",
            "tenant_id",
            "owner_user_id",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'private'")
    )
    embedding_profile_key: Mapped[str] = mapped_column(String, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    collection_name: Mapped[str] = mapped_column(String, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hops: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── knowledge_documents ──────────────────────────────────────────────

class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index(
            "knowledge_documents_tenant_kb_idx",
            "tenant_id",
            "kb_id",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_documents_active_content_hash_idx",
            "kb_id",
            "content_hash",
            unique=True,
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_documents_directory_idx",
            "kb_id",
            "directory",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)
    stored_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    stored_sha256: Mapped[str | None] = mapped_column(String)
    content_encoding: Mapped[str | None] = mapped_column(String)
    upload_id: Mapped[str | None] = mapped_column(String)
    directory: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("''")
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── knowledge_chunks ─────────────────────────────────────────────────

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("knowledge_chunks_document_ordinal_idx", "document_id", "ordinal", unique=True),
        Index("knowledge_chunks_vector_point_idx", "vector_point_id", unique=True),
        Index(
            "knowledge_chunks_tenant_kb_document_idx",
            "tenant_id",
            "kb_id",
            "document_id",
            "ordinal",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text_: Mapped[str] = mapped_column("text", String, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, server_default=sa_text("'{}'")
    )
    vector_point_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── graph_entities ───────────────────────────────────────────────────

class GraphEntity(Base):
    __tablename__ = "graph_entities"
    __table_args__ = (
        Index(
            "graph_entities_kb_document_key_idx",
            "kb_id",
            "document_id",
            "entity_key",
            unique=True,
        ),
        Index("graph_entities_tenant_kb_key_idx", "tenant_id", "kb_id", "entity_key"),
        Index("graph_entities_chunk_ids_gin_idx", "chunk_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=False
    )
    entity_key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── graph_relationships ──────────────────────────────────────────────

class GraphRelationship(Base):
    __tablename__ = "graph_relationships"
    __table_args__ = (
        Index("graph_relationships_tenant_kb_source_idx", "tenant_id", "kb_id", "source_key"),
        Index("graph_relationships_tenant_kb_target_idx", "tenant_id", "kb_id", "target_key"),
        Index("graph_relationships_chunk_ids_gin_idx", "chunk_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    target_key: Mapped[str] = mapped_column(String, nullable=False)
    relation: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa_text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── knowledge_index_jobs ─────────────────────────────────────────────

class KnowledgeIndexJob(Base):
    __tablename__ = "knowledge_index_jobs"
    __table_args__ = (
        Index(
            "knowledge_index_jobs_tenant_status_idx",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index("knowledge_index_jobs_document_idx", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── knowledge_retrieval_logs ─────────────────────────────────────────

class KnowledgeRetrievalLog(Base):
    __tablename__ = "knowledge_retrieval_logs"
    __table_args__ = (
        Index(
            "knowledge_retrieval_logs_tenant_run_idx",
            "tenant_id",
            "run_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    retrieval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False
    )
    kb_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    query: Mapped[str] = mapped_column(String, nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hops: Mapped[int] = mapped_column(Integer, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rerank_status: Mapped[str] = mapped_column(String, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── artifacts ────────────────────────────────────────────────────────

class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("artifacts_tenant_object_key_idx", "tenant_id", "object_key", unique=True),
        Index("artifacts_tenant_status_idx", "tenant_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── chat_attachments ─────────────────────────────────────────────────

class ChatAttachment(Base):
    __tablename__ = "chat_attachments"
    __table_args__ = (
        Index("chat_attachments_tenant_object_key_idx", "tenant_id", "object_key"),
        Index("chat_attachments_user_idx", "tenant_id", "user_id", "created_at"),
        Index("chat_attachments_run_idx", "run_id"),
        Index("chat_attachments_dedup_idx", "tenant_id", "content_sha256"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id")
    )
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    content_encoding: Mapped[str | None] = mapped_column(String)
    upload_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── run_events ───────────────────────────────────────────────────────

class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "seq"),
        Index("run_events_tenant_run_idx", "tenant_id", "run_id", "seq"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── run_dispatch_outbox ──────────────────────────────────────────────

class RunDispatchOutbox(Base):
    __tablename__ = "run_dispatch_outbox"
    __table_args__ = (
        Index("run_dispatch_outbox_pending_idx", "available_at", "created_at"),
        Index("run_dispatch_outbox_run_idx", "tenant_id", "run_id", "created_at"),
        Index("run_dispatch_outbox_unconsumed_idx", "published_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False
    )
    job_kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


# ─── knowledge_assets ─────────────────────────────────────────────────

class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"
    __table_args__ = (
        Index(
            "knowledge_assets_tenant_kb_idx",
            "tenant_id",
            "kb_id",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_assets_document_idx",
            "tenant_id",
            "document_id",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_assets_rel_path_idx",
            "kb_id",
            "rel_path",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index(
            "knowledge_assets_active_hash_idx",
            "kb_id",
            "content_hash",
            unique=True,
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="cascade"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="cascade"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="set null")
    )
    rel_path: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    stored_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    stored_sha256: Mapped[str | None] = mapped_column(String)
    content_encoding: Mapped[str | None] = mapped_column(String)
    upload_id: Mapped[str | None] = mapped_column(String)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    caption: Mapped[str | None] = mapped_column(String)
    caption_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=sa_text("'pending'")
    )
    caption_model: Mapped[str | None] = mapped_column(String)
    caption_error: Mapped[str | None] = mapped_column(String)
    caption_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    caption_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, server_default=sa_text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─── knowledge_caption_jobs ───────────────────────────────────────────

class KnowledgeCaptionJob(Base):
    __tablename__ = "knowledge_caption_jobs"
    __table_args__ = (
        Index(
            "knowledge_caption_jobs_active_asset_idx",
            "asset_id",
            unique=True,
            postgresql_where=sa_text("status IN ('queued', 'running')"),
        ),
        Index("knowledge_caption_jobs_claim_idx", "status", "next_attempt_at", "created_at"),
        Index("knowledge_caption_jobs_tenant_kb_idx", "tenant_id", "kb_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="cascade"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="cascade"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_assets.id", ondelete="cascade"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("3")
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
