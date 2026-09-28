"""AgentRepository — raw asyncpg SQL port of packages/db/src/repository.ts.

All 74 public methods are present and use ``$1, $2`` positional parameters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
from contracts import (
    AgentEvent,
    AuthContext,
    ObservabilityContext,
    WorkspaceSource,
)

# ─── Record types (Pydantic) ──────────────────────────────────────────
from pydantic import BaseModel

from ._json import json_dumps
from .errors import RepositoryConflictError, RepositoryNotFoundError


class SessionRecord(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    title: str
    external_key: str | None
    project_id: str | None
    workspace_id: str
    workspace_path: str
    approval_mode: str
    created_at: str
    updated_at: str


class ProjectRecord(BaseModel):
    id: str
    tenant_id: str
    name: str
    source_type: str
    source_ref: str | None
    source_revision: str | None
    created_at: str
    updated_at: str


class SessionListCursor(BaseModel):
    updated_at: str
    id: str


class WorkspaceSandboxRecord(BaseModel):
    workspace_id: str
    sandbox_id: str | None
    sandbox_provider: str


class RunRecord(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    session_id: str
    status: str
    user_message: str
    continuation: bool
    knowledge_base_ids: list[str]
    last_event_seq: int
    cancel_requested_at: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str


class InterruptRecord(BaseModel):
    id: str
    run_id: str
    kind: str
    request: Any
    response: Any


class CancellationResult(BaseModel):
    run: RunRecord
    event: dict[str, Any] | None


class MemoryJobRecord(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    attempts: int


class ArtifactRecord(BaseModel):
    id: str
    tenant_id: str
    run_id: str
    name: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    created_at: str
    uploaded_at: str | None


class ChatAttachmentRecord(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    run_id: str | None
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content_sha256: str
    content_encoding: str | None
    upload_id: str | None
    kind: str
    status: str
    created_at: str
    uploaded_at: str | None


class DispatchOutboxRecord(BaseModel):
    id: str
    tenant_id: str
    run_id: str
    job: dict[str, Any]
    attempts: int


class MemoryRecord(BaseModel):
    # id/时间戳由 DB 在写入时生成；作为 upsert 输入时可省略（空串即"未指定"）。
    id: str = ""
    tenant_id: str
    user_id: str
    project_id: str | None
    assistant_key: str
    scope: str
    kind: str
    content: str
    normalized_key: str
    importance: float
    confidence: float
    status: str
    source_session_id: str | None
    source_run_id: str | None
    supersedes_id: str | None
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str | None = None
    metadata: dict[str, Any] = {}


class MemoryListInput(BaseModel):
    tenant_id: str
    user_id: str
    assistant_key: str
    scope: str | None = None
    project_id: str | None = None
    status: str | None = None
    limit: int = 100


class MemoryJobInput(BaseModel):
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str


# ─── helpers ──────────────────────────────────────────────────────────

def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _session_of(row: asyncpg.Record) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        title=str(row["title"]),
        external_key=str(row["external_key"]) if row["external_key"] else None,
        project_id=str(row["project_id"]) if row["project_id"] else None,
        workspace_id=str(row["workspace_id"]),
        workspace_path=str(row["workspace_path"]),
        approval_mode=str(row["approval_mode"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _project_of(row: asyncpg.Record) -> ProjectRecord:
    return ProjectRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"]),
        source_type=str(row["source_type"]),
        source_ref=str(row["source_ref"]) if row["source_ref"] else None,
        source_revision=str(row["source_revision"]) if row["source_revision"] else None,
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _workspace_source_of(row: asyncpg.Record) -> WorkspaceSource | None:
    st = row.get("source_type")
    if not st or st == "empty":
        return None
    if st == "git" and row.get("source_ref"):
        from contracts import GitProjectSource
        return GitProjectSource(
            url=str(row["source_ref"]),
            ref=str(row["source_revision"]) if row.get("source_revision") else None,
        )
    if st == "upload" and row.get("source_ref"):
        from contracts import UploadProjectSource
        return UploadProjectSource(object_key=str(row["source_ref"]))
    return None


def _run_of(row: asyncpg.Record) -> RunRecord:
    return RunRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        status=str(row["status"]),
        user_message=str(row["user_message"]),
        continuation=bool(row["continuation"]),
        knowledge_base_ids=[str(x) for x in row["knowledge_base_ids"]] if row["knowledge_base_ids"] else [],
        last_event_seq=int(row["last_event_seq"]),
        cancel_requested_at=_iso(row["cancel_requested_at"]) if row["cancel_requested_at"] else None,
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        error_message=str(row["error_message"]) if row["error_message"] is not None else None,
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _artifact_of(row: asyncpg.Record) -> ArtifactRecord:
    return ArtifactRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        run_id=str(row["run_id"]),
        name=str(row["name"]),
        object_key=str(row["object_key"]),
        content_type=str(row["content_type"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        status=str(row["status"]),
        created_at=_iso(row["created_at"]),
        uploaded_at=_iso(row["uploaded_at"]) if row["uploaded_at"] else None,
    )


def _chat_attachment_of(row: asyncpg.Record) -> ChatAttachmentRecord:
    return ChatAttachmentRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        run_id=str(row["run_id"]) if row["run_id"] else None,
        object_key=str(row["object_key"]),
        filename=str(row["filename"]),
        content_type=str(row["content_type"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        content_sha256=str(row.get("content_sha256", row["sha256"])),
        content_encoding=str(row["content_encoding"]) if row["content_encoding"] else None,
        upload_id=str(row["upload_id"]) if row["upload_id"] else None,
        kind=str(row["kind"]),
        status=str(row["status"]),
        created_at=_iso(row["created_at"]),
        uploaded_at=_iso(row["uploaded_at"]) if row["uploaded_at"] else None,
    )


def _memory_of(row: asyncpg.Record) -> MemoryRecord:
    return MemoryRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        project_id=str(row["project_id"]) if row["project_id"] else None,
        assistant_key=str(row["assistant_key"]),
        scope=str(row["scope"]),
        kind=str(row["kind"]),
        content=str(row["content"]),
        normalized_key=str(row["normalized_key"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        status=str(row["status"]),
        source_session_id=str(row["source_session_id"]) if row["source_session_id"] else None,
        source_run_id=str(row["source_run_id"]) if row["source_run_id"] else None,
        supersedes_id=str(row["supersedes_id"]) if row["supersedes_id"] else None,
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        last_accessed_at=_iso(row["last_accessed_at"]) if row["last_accessed_at"] else None,
        metadata=row["metadata"] if isinstance(row["metadata"], dict) else {},
    )


# ─── AgentRepository ──────────────────────────────────────────────────

class AgentRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ── health ────────────────────────────────────────────────────────

    async def ping(self) -> None:
        await self._pool.fetchval("SELECT 1")

    async def close(self) -> None:
        await self._pool.close()

    # ── memories ──────────────────────────────────────────────────────

    async def list_memories(self, input: MemoryListInput) -> list[MemoryRecord]:
        values: list[Any] = [input.tenant_id, input.user_id, input.assistant_key]
        predicates = [
            "tenant_id = $1",
            "user_id = $2",
            "assistant_key = $3",
        ]
        if input.scope is not None:
            values.append(input.scope)
            predicates.append(f"scope = ${len(values)}")
        if input.project_id is not None:
            values.append(input.project_id)
            predicates.append(f"project_id IS NOT DISTINCT FROM ${len(values)}")
        values.append(input.status or "active")
        predicates.append(f"status = ${len(values)}")
        limit = max(1, min(input.limit or 100, 500))
        values.append(limit)
        rows = await self._pool.fetch(
            f"""SELECT * FROM agent_memories
                WHERE {" AND ".join(predicates)}
                ORDER BY importance DESC, updated_at DESC, id DESC
                LIMIT ${len(values)}""",
            *values,
        )
        return [_memory_of(r) for r in rows]

    async def get_memory(self, tenant_id: str, user_id: str, id: str) -> MemoryRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_memories WHERE tenant_id = $1 AND user_id = $2 AND id = $3",
            tenant_id, user_id, id,
        )
        return _memory_of(row) if row else None

    async def upsert_memory(
        self,
        input: MemoryRecord,
    ) -> MemoryRecord:
        mem_id = input.id or str(uuid.uuid4())
        row = await self._pool.fetchrow(
            """INSERT INTO agent_memories
                (id, tenant_id, user_id, project_id, assistant_key, scope, kind, content,
                 normalized_key, importance, confidence, status, source_session_id,
                 source_run_id, supersedes_id, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb)
               ON CONFLICT (tenant_id, user_id, assistant_key, scope, normalized_key)
                 WHERE status = 'active'
               DO UPDATE SET content = EXCLUDED.content,
                             kind = EXCLUDED.kind,
                             importance = EXCLUDED.importance,
                             confidence = EXCLUDED.confidence,
                             source_session_id = EXCLUDED.source_session_id,
                             source_run_id = EXCLUDED.source_run_id,
                             supersedes_id = EXCLUDED.supersedes_id,
                             metadata = EXCLUDED.metadata,
                             updated_at = now()
               RETURNING *""",
            mem_id,
            input.tenant_id,
            input.user_id,
            input.project_id,
            input.assistant_key,
            input.scope,
            input.kind,
            input.content,
            input.normalized_key,
            input.importance,
            input.confidence,
            input.status,
            input.source_session_id,
            input.source_run_id,
            input.supersedes_id,
            json_dumps(input.metadata or {}),
        )
        return _memory_of(row)

    async def delete_memory(self, tenant_id: str, user_id: str, id: str) -> None:
        await self._pool.execute(
            """UPDATE agent_memories SET status = 'deleted', updated_at = now()
               WHERE tenant_id = $1 AND user_id = $2 AND id = $3""",
            tenant_id, user_id, id,
        )

    async def update_memory(
        self, tenant_id: str, user_id: str, id: str, content: str
    ) -> MemoryRecord | None:
        row = await self._pool.fetchrow(
            """UPDATE agent_memories SET content = $4, updated_at = now()
               WHERE tenant_id = $1 AND user_id = $2 AND id = $3 AND status = 'active'
               RETURNING *""",
            tenant_id, user_id, id, content.strip()[:2000],
        )
        return _memory_of(row) if row else None

    async def clear_memories(self, tenant_id: str, user_id: str, assistant_key: str | None = None) -> None:
        values: list[Any] = [tenant_id, user_id]
        assistant_predicate = " AND assistant_key = $3" if assistant_key else ""
        if assistant_key:
            values.append(assistant_key)
        await self._pool.execute(
            f"""UPDATE agent_memories SET status = 'deleted', updated_at = now()
                WHERE tenant_id = $1 AND user_id = $2{assistant_predicate}""",
            *values,
        )

    # ── memory jobs ───────────────────────────────────────────────────

    async def enqueue_memory_job(self, input: MemoryJobInput) -> str:
        row = await self._pool.fetchrow(
            """INSERT INTO memory_jobs (id, tenant_id, user_id, session_id, run_id)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (tenant_id, run_id)
               DO UPDATE SET updated_at = now()
               RETURNING id""",
            uuid.uuid4(), input.tenant_id, input.user_id, input.session_id, input.run_id,
        )
        return str(row["id"])

    async def claim_memory_job(self, lease_ms: int = 300_000) -> MemoryJobRecord | None:
        row = await self._pool.fetchrow(
            """WITH candidate AS (
                 SELECT id FROM memory_jobs
                 WHERE (status = 'queued' AND available_at <= now())
                    OR (status = 'processing' AND locked_at < now() - ($1::integer * interval '1 millisecond'))
                 ORDER BY created_at ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
               )
               UPDATE memory_jobs AS job
               SET status = 'processing', locked_at = now(), attempts = job.attempts + 1,
                   updated_at = now()
               FROM candidate
               WHERE job.id = candidate.id
               RETURNING job.id, job.tenant_id, job.user_id, job.session_id, job.run_id, job.attempts""",
            lease_ms,
        )
        if not row:
            return None
        return MemoryJobRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            user_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            attempts=int(row["attempts"]),
        )

    async def complete_memory_job(self, id: str) -> None:
        await self._pool.execute(
            """UPDATE memory_jobs SET status = 'completed', locked_at = NULL, updated_at = now()
               WHERE id = $1""",
            id,
        )

    async def fail_memory_job(self, id: str, error: str, delay_ms: int = 30_000) -> None:
        await self._pool.execute(
            """UPDATE memory_jobs SET status = 'queued', locked_at = NULL,
                   available_at = now() + ($2::integer * interval '1 millisecond'),
                   last_error = left($3, 4000), updated_at = now()
               WHERE id = $1""",
            id, delay_ms, error,
        )

    # ── identity / auth ───────────────────────────────────────────────

    async def ensure_identity(self, context: AuthContext) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "BEGIN"
            )
            try:
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, 'Agent tenant') ON CONFLICT (id) DO NOTHING",
                    context.tenant_id,
                )
                await conn.execute(
                    "INSERT INTO users (id, display_name) VALUES ($1, 'Agent user') ON CONFLICT (id) DO NOTHING",
                    context.user_id,
                )
                await conn.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
                    context.tenant_id, context.user_id, context.roles[0] if context.roles else "member",
                )
                await conn.execute("COMMIT")
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def get_membership_role(self, tenant_id: str, user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
            tenant_id, user_id,
        )
        return str(row["role"]) if row else None

    async def find_user_for_login(
        self, username: str
    ) -> dict[str, str] | None:
        row = await self._pool.fetchrow(
            """SELECT u.id, u.display_name, u.password_hash, tm.tenant_id, tm.role
               FROM users u
               JOIN tenant_memberships tm ON tm.user_id = u.id
               WHERE u.username = $1
               ORDER BY tm.created_at ASC
               LIMIT 1""",
            username,
        )
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "display_name": str(row["display_name"]),
            "password_hash": str(row["password_hash"]) if row["password_hash"] else None,
            "tenant_id": str(row["tenant_id"]),
            "role": str(row["role"]),
        }

    async def get_user_display_name(self, tenant_id: str, user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            """SELECT u.display_name FROM users u
               JOIN tenant_memberships tm ON tm.user_id = u.id
               WHERE u.id = $2 AND tm.tenant_id = $1""",
            tenant_id, user_id,
        )
        return str(row["display_name"]) if row else None

    async def get_user_avatar_url(self, tenant_id: str, user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            """SELECT u.avatar_url FROM users u
               JOIN tenant_memberships tm ON tm.user_id = u.id
               WHERE u.id = $2 AND tm.tenant_id = $1""",
            tenant_id, user_id,
        )
        return str(row["avatar_url"]) if row and row["avatar_url"] else None

    async def update_user_avatar_url(self, user_id: str, avatar_url: str) -> None:
        await self._pool.execute(
            "UPDATE users SET avatar_url = $1 WHERE id = $2",
            avatar_url, user_id,
        )

    async def get_user_password_hash(self, tenant_id: str, user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            """SELECT u.password_hash
               FROM users u
               JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.tenant_id = $1
               WHERE u.id = $2""",
            tenant_id, user_id,
        )
        if not row:
            return None
        return str(row["password_hash"]) if row["password_hash"] else None

    async def list_tenant_users(
        self, tenant_id: str
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """SELECT u.id, u.username, u.display_name, tm.role, tm.created_at,
                      (SELECT count(*)::int FROM knowledge_base_grants g
                        WHERE g.tenant_id = tm.tenant_id AND g.user_id = u.id) AS granted_kb_count
               FROM users u
               JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.tenant_id = $1
               ORDER BY tm.created_at ASC, u.id ASC""",
            tenant_id,
        )
        return [
            {
                "id": str(r["id"]),
                "username": str(r["username"]) if r["username"] else None,
                "display_name": str(r["display_name"]),
                "role": str(r["role"]),
                "granted_kb_count": int(r["granted_kb_count"]),
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ]

    async def create_tenant_user(
        self, tenant_id: str,
        input: dict[str, Any]
    ) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                user_id = input.get("id") or str(uuid.uuid4())
                user_row = await conn.fetchrow(
                    """INSERT INTO users (id, username, display_name, password_hash)
                       VALUES ($1, $2, $3, $4)
                       RETURNING id, username, display_name""",
                    user_id, input["username"], input["display_name"], input["password_hash"],
                )
                await conn.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, $3)
                       ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
                    tenant_id, user_id, input["role"],
                )
                await conn.execute("COMMIT")
                return {
                    "id": str(user_row["id"]),
                    "username": str(user_row["username"]),
                    "display_name": str(user_row["display_name"]),
                    "role": input["role"],
                }
            except asyncpg.exceptions.UniqueViolationError:
                await conn.execute("ROLLBACK")
                raise RepositoryConflictError("username_taken")
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def update_tenant_user(
        self, tenant_id: str, user_id: str,
        patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                membership = await conn.fetchrow(
                    "SELECT role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                if not membership:
                    await conn.execute("COMMIT")
                    return None

                if patch.get("role") is not None:
                    await conn.execute(
                        "UPDATE tenant_memberships SET role = $3 WHERE tenant_id = $1 AND user_id = $2",
                        tenant_id, user_id, patch["role"],
                    )
                if patch.get("display_name") is not None or patch.get("password_hash") is not None:
                    await conn.execute(
                        """UPDATE users
                           SET display_name = COALESCE($2, display_name),
                               password_hash = COALESCE($3, password_hash)
                           WHERE id = $1""",
                        user_id,
                        patch.get("display_name"),
                        patch.get("password_hash"),
                    )

                user = await conn.fetchrow("SELECT id, username, display_name FROM users WHERE id = $1", user_id)
                if not user:
                    await conn.execute("COMMIT")
                    return None
                role_row = await conn.fetchrow(
                    "SELECT role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                if not role_row:
                    await conn.execute("COMMIT")
                    return None
                await conn.execute("COMMIT")
                return {
                    "id": str(user["id"]),
                    "username": str(user["username"]) if user["username"] else None,
                    "display_name": str(user["display_name"]),
                    "role": str(role_row["role"]),
                }
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def delete_user(self, tenant_id: str, user_id: str) -> dict[str, list[str]] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                membership = await conn.fetchrow(
                    "SELECT 1 FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                if not membership:
                    await conn.execute("COMMIT")
                    return None

                owned_kbs = await conn.fetchrow(
                    """SELECT 1 FROM knowledge_bases
                       WHERE tenant_id = $1 AND owner_user_id = $2 AND deleted_at IS NULL LIMIT 1""",
                    tenant_id, user_id,
                )
                if owned_kbs:
                    await conn.execute("ROLLBACK")
                    raise RepositoryConflictError("user_owns_knowledge_bases")

                artifact_keys = await conn.fetch(
                    """SELECT a.object_key FROM artifacts a
                       JOIN agent_runs r ON r.id = a.run_id
                       WHERE r.tenant_id = $1 AND r.user_id = $2""",
                    tenant_id, user_id,
                )
                deleted_artifact_keys = [str(r["object_key"]) for r in artifact_keys]

                run_ids = await conn.fetch(
                    "SELECT id FROM agent_runs WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                run_id_list = [str(r["id"]) for r in run_ids]

                if run_id_list:
                    await conn.execute(
                        "DELETE FROM run_events WHERE tenant_id = $1 AND run_id = ANY($2::uuid[])",
                        tenant_id, run_id_list,
                    )
                    await conn.execute(
                        "DELETE FROM run_dispatch_outbox WHERE tenant_id = $1 AND run_id = ANY($2::uuid[])",
                        tenant_id, run_id_list,
                    )
                    await conn.execute(
                        "DELETE FROM artifacts WHERE tenant_id = $1 AND run_id = ANY($2::uuid[])",
                        tenant_id, run_id_list,
                    )

                await conn.execute(
                    "DELETE FROM knowledge_retrieval_logs WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                await conn.execute(
                    "DELETE FROM agent_runs WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                await conn.execute(
                    "DELETE FROM agent_sessions WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                await conn.execute(
                    "DELETE FROM knowledge_base_grants WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                await conn.execute(
                    "DELETE FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                await conn.execute("DELETE FROM users WHERE id = $1", user_id)
                await conn.execute("COMMIT")
                return {"deleted_artifact_keys": deleted_artifact_keys}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    # ── OAuth ─────────────────────────────────────────────────────────

    async def find_oauth_account(
        self, provider: str, subject: str
    ) -> dict[str, str] | None:
        row = await self._pool.fetchrow(
            """SELECT oa.user_id, u.display_name, tm.tenant_id, tm.role
               FROM oauth_accounts oa
               JOIN users u ON u.id = oa.user_id
               JOIN tenant_memberships tm ON tm.user_id = oa.user_id
               WHERE oa.provider = $1 AND oa.subject = $2
               ORDER BY tm.created_at ASC
               LIMIT 1""",
            provider, subject,
        )
        if not row:
            return None
        return {
            "user_id": str(row["user_id"]),
            "display_name": str(row["display_name"]),
            "tenant_id": str(row["tenant_id"]),
            "role": str(row["role"]),
        }

    async def create_oauth_user(
        self, tenant_id: str,
        input: dict[str, Any]
    ) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                user_id = uuid.uuid4()
                account_id = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO users (id, display_name, avatar_url) VALUES ($1, $2, $3)",
                    user_id, input["display_name"], input.get("avatar_url"),
                )
                await conn.execute(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, $3)",
                    tenant_id, user_id, input["role"],
                )
                await conn.execute(
                    """INSERT INTO oauth_accounts
                       (id, user_id, provider, subject, email, display_name, avatar_url)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    account_id, user_id, input["provider"], input["subject"],
                    input.get("email"), input["display_name"], input.get("avatar_url"),
                )
                await conn.execute("COMMIT")
                return {"id": str(user_id), "display_name": input["display_name"], "role": input["role"]}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def link_oauth_account(
        self, user_id: str, input: dict[str, Any]
    ) -> None:
        await self._pool.execute(
            """INSERT INTO oauth_accounts
               (id, user_id, provider, subject, email, display_name, avatar_url)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            uuid.uuid4(), user_id, input["provider"], input["subject"],
            input.get("email"), input["display_name"], input.get("avatar_url"),
        )

    # ── projects ──────────────────────────────────────────────────────

    async def create_project(
        self, context: AuthContext, input: dict[str, Any]
    ) -> ProjectRecord:
        project_id = input.get("id") or str(uuid.uuid4())
        row = await self._pool.fetchrow(
            """INSERT INTO projects
               (id, tenant_id, name, source_type, source_ref, source_revision)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING *""",
            project_id, context.tenant_id, input["name"],
            input["source_type"], input.get("source_ref"), input.get("source_revision"),
        )
        return _project_of(row)

    async def list_projects(self, context: AuthContext) -> list[ProjectRecord]:
        rows = await self._pool.fetch(
            """SELECT * FROM projects
               WHERE tenant_id = $1
               ORDER BY updated_at DESC, id DESC
               LIMIT 100""",
            context.tenant_id,
        )
        return [_project_of(r) for r in rows]

    async def get_project(self, context: AuthContext, project_id: str) -> ProjectRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM projects WHERE tenant_id = $1 AND id = $2",
            context.tenant_id, project_id,
        )
        return _project_of(row) if row else None

    # ── sessions ──────────────────────────────────────────────────────

    async def create_session(
        self, context: AuthContext, input: dict[str, Any]
    ) -> SessionRecord:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                if input.get("project_id"):
                    project = await conn.fetchrow(
                        "SELECT 1 FROM projects WHERE tenant_id = $1 AND id = $2",
                        context.tenant_id, input["project_id"],
                    )
                    if not project:
                        raise RepositoryNotFoundError("project")
                workspace_id = uuid.uuid4()
                session_id = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO workspaces (id, tenant_id, project_id, path) VALUES ($1, $2, $3, $4)",
                    workspace_id, context.tenant_id, input.get("project_id"), input["workspace_path"],
                )
                row = await conn.fetchrow(
                    """INSERT INTO agent_sessions
                       (id, tenant_id, user_id, project_id, workspace_id, external_key, title)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       RETURNING *, $8::text AS workspace_path""",
                    session_id, context.tenant_id, context.user_id,
                    input.get("project_id"), workspace_id,
                    input.get("external_key"), input["title"], input["workspace_path"],
                )
                await conn.execute("COMMIT")
                return _session_of(row)
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def get_or_create_external_session(
        self, context: AuthContext, input: dict[str, Any]
    ) -> SessionRecord:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"{context.tenant_id}:{context.user_id}:{input['external_key']}",
                )
                existing = await conn.fetchrow(
                    """SELECT s.*, w.path AS workspace_path
                       FROM agent_sessions s
                       JOIN workspaces w ON w.id = s.workspace_id
                       WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.external_key = $3
                         AND s.deleted_at IS NULL""",
                    context.tenant_id, context.user_id, input["external_key"],
                )
                if existing:
                    if str(existing["title"]) == "新会话" and input["title"] != "新会话":
                        renamed = await conn.fetchrow(
                            """UPDATE agent_sessions
                               SET title = $4, updated_at = now()
                               WHERE tenant_id = $1 AND user_id = $2 AND id = $3
                               RETURNING *, $5::text AS workspace_path""",
                            context.tenant_id, context.user_id,
                            str(existing["id"]), input["title"], str(existing["workspace_path"]),
                        )
                        await conn.execute("COMMIT")
                        return _session_of(renamed)
                    await conn.execute("COMMIT")
                    return _session_of(existing)

                if input.get("project_id"):
                    project = await conn.fetchrow(
                        "SELECT 1 FROM projects WHERE tenant_id = $1 AND id = $2",
                        context.tenant_id, input["project_id"],
                    )
                    if not project:
                        raise RepositoryNotFoundError("project")

                workspace_id = uuid.uuid4()
                session_id = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO workspaces (id, tenant_id, project_id, path) VALUES ($1, $2, $3, $4)",
                    workspace_id, context.tenant_id, input.get("project_id"), input["workspace_path"],
                )
                created = await conn.fetchrow(
                    """INSERT INTO agent_sessions
                       (id, tenant_id, user_id, project_id, workspace_id, external_key, title)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       RETURNING *, $8::text AS workspace_path""",
                    session_id, context.tenant_id, context.user_id,
                    input.get("project_id"), workspace_id,
                    input["external_key"], input["title"], input["workspace_path"],
                )
                await conn.execute("COMMIT")
                return _session_of(created)
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def list_sessions(
        self, context: AuthContext, input: dict[str, Any]
    ) -> list[SessionRecord]:
        cursor = input.get("cursor")
        rows = await self._pool.fetch(
            """SELECT s.*, w.path AS workspace_path
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.deleted_at IS NULL
                 AND (
                   $3::timestamptz IS NULL
                   OR (s.updated_at, s.id) < ($3::timestamptz, $4::uuid)
                 )
               ORDER BY s.updated_at DESC, s.id DESC
               LIMIT $5""",
            context.tenant_id, context.user_id,
            cursor["updated_at"] if cursor else None,
            cursor["id"] if cursor else None,
            input["limit"],
        )
        return [_session_of(r) for r in rows]

    async def get_session(self, context: AuthContext, session_id: str) -> SessionRecord | None:
        row = await self._pool.fetchrow(
            """SELECT s.*, w.path AS workspace_path
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.id = $3
                 AND s.deleted_at IS NULL""",
            context.tenant_id, context.user_id, session_id,
        )
        return _session_of(row) if row else None

    async def get_session_by_external_key(
        self, context: AuthContext, external_key: str
    ) -> SessionRecord | None:
        row = await self._pool.fetchrow(
            """SELECT s.*, w.path AS workspace_path
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.external_key = $3
                 AND s.deleted_at IS NULL""",
            context.tenant_id, context.user_id, external_key,
        )
        return _session_of(row) if row else None

    async def rename_session(
        self, context: AuthContext, session_id: str, title: str
    ) -> SessionRecord | None:
        row = await self._pool.fetchrow(
            """UPDATE agent_sessions AS s
               SET title = $4, updated_at = now()
               FROM workspaces w
               WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.id = $3
                 AND s.deleted_at IS NULL AND w.id = s.workspace_id
               RETURNING s.*, w.path AS workspace_path""",
            context.tenant_id, context.user_id, session_id, title,
        )
        return _session_of(row) if row else None

    async def delete_session(
        self, context: AuthContext, session_id: str
    ) -> str:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                session = await conn.fetchrow(
                    """SELECT id FROM agent_sessions
                       WHERE tenant_id = $1 AND user_id = $2 AND id = $3
                         AND deleted_at IS NULL
                       FOR UPDATE""",
                    context.tenant_id, context.user_id, session_id,
                )
                if not session:
                    await conn.execute("COMMIT")
                    return "not_found"
                active_run = await conn.fetchrow(
                    """SELECT 1 FROM agent_runs
                       WHERE tenant_id = $1 AND session_id = $2
                         AND status IN ('queued', 'running', 'waiting_approval', 'waiting_question')
                       LIMIT 1""",
                    context.tenant_id, session_id,
                )
                if active_run:
                    await conn.execute("COMMIT")
                    return "active"
                await conn.execute(
                    """UPDATE agent_sessions
                       SET deleted_at = now(), updated_at = now()
                       WHERE tenant_id = $1 AND user_id = $2 AND id = $3""",
                    context.tenant_id, context.user_id, session_id,
                )
                await conn.execute("COMMIT")
                return "deleted"
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    # ── runs ──────────────────────────────────────────────────────────

    async def list_session_runs(self, context: AuthContext, session_id: str) -> list[RunRecord]:
        rows = await self._pool.fetch(
            """SELECT * FROM agent_runs
               WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
               ORDER BY created_at ASC""",
            context.tenant_id, context.user_id, session_id,
        )
        return [_run_of(r) for r in rows]

    async def create_run(
        self, context: AuthContext, input: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                if input.get("idempotency_key"):
                    existing = await conn.fetchrow(
                        "SELECT * FROM agent_runs WHERE tenant_id = $1 AND idempotency_key = $2",
                        context.tenant_id, input["idempotency_key"],
                    )
                    if existing:
                        await conn.execute("COMMIT")
                        return {"run": _run_of(existing), "created": False}

                session = await conn.fetchrow(
                    """SELECT s.id, s.approval_mode, w.path AS workspace_path,
                              p.source_type, p.source_ref, p.source_revision
                       FROM agent_sessions s
                       JOIN workspaces w ON w.id = s.workspace_id
                       LEFT JOIN projects p ON p.id = s.project_id AND p.tenant_id = s.tenant_id
                       WHERE s.tenant_id = $1 AND s.user_id = $2 AND s.id = $3
                         AND s.deleted_at IS NULL
                       FOR UPDATE OF s""",
                    context.tenant_id, context.user_id, input["session_id"],
                )
                if not session:
                    raise RepositoryNotFoundError("session")

                kb_ids = list(dict.fromkeys(input.get("knowledge_base_ids", [])))
                if kb_ids:
                    visible = await conn.fetch(
                        """SELECT id FROM knowledge_bases
                           WHERE tenant_id = $1 AND deleted_at IS NULL AND id = ANY($2::uuid[])
                             AND (visibility = 'tenant' OR owner_user_id = $3
                               OR EXISTS (SELECT 1 FROM knowledge_base_grants g
                                          WHERE g.kb_id = knowledge_bases.id AND g.user_id = $3 AND g.tenant_id = $1)
                               OR $4::text[] && ARRAY['admin']::text[])""",
                        context.tenant_id, kb_ids, context.user_id, context.roles,
                    )
                    if len(visible) != len(kb_ids):
                        raise RepositoryNotFoundError("knowledge_base")

                run_id = uuid.uuid4()
                result = await conn.fetchrow(
                    """INSERT INTO agent_runs
                       (id, tenant_id, user_id, session_id, status, user_message, continuation,
                        knowledge_base_ids, idempotency_key)
                       VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7::uuid[], $8)
                       RETURNING *""",
                    run_id, context.tenant_id, context.user_id, input["session_id"],
                    input["message"], input.get("continuation", False), kb_ids,
                    input.get("idempotency_key"),
                )
                await conn.execute(
                    "UPDATE agent_sessions SET updated_at = now() WHERE id = $1",
                    input["session_id"],
                )
                attachments = input.get("attachments", [])
                if attachments:
                    attachment_ids = [a["id"] for a in attachments]
                    linked = await conn.execute(
                        """UPDATE chat_attachments
                           SET run_id = $1
                           WHERE tenant_id = $2 AND user_id = $3 AND status = 'ready'
                             AND run_id IS NULL AND id = ANY($4::uuid[])""",
                        run_id, context.tenant_id, context.user_id, attachment_ids,
                    )
                    if linked != "UPDATE 0" and int(linked.split(" ")[1]) != len(attachment_ids):
                        raise RepositoryNotFoundError("chat_attachment")

                run = _run_of(result)
                ws_source = _workspace_source_of(session)
                job_payload: dict[str, Any] = {
                    "kind": "start",
                    "tenantId": context.tenant_id,
                    "userId": context.user_id,
                    "sessionId": input["session_id"],
                    "runId": str(run_id),
                    "message": input["message"],
                    "workspacePath": str(session["workspace_path"]),
                    "approvalMode": str(session["approval_mode"]),
                    "knowledgeBaseIds": kb_ids,
                    "attachments": attachments,
                }
                if ws_source:
                    job_payload["workspaceSource"] = ws_source.model_dump(by_alias=True)
                if input.get("observability_context"):
                    job_payload["observability"] = input["observability_context"]

                # We need to construct a RunJob from the dict for insert_dispatch.
                # For simplicity, insert the raw JSON directly.
                outbox_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO run_dispatch_outbox (id, tenant_id, run_id, job_kind, payload)
                       VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    outbox_id, context.tenant_id, run_id, "start",
                    json_dumps(job_payload),
                )
                await conn.execute("COMMIT")
                return {"run": run, "created": True, "outbox_id": outbox_id}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def get_run(self, context: AuthContext, run_id: str) -> RunRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_runs WHERE tenant_id = $1 AND id = $2",
            context.tenant_id, run_id,
        )
        return _run_of(row) if row else None

    async def get_run_for_worker(self, tenant_id: str, run_id: str) -> RunRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_runs WHERE tenant_id = $1 AND id = $2",
            tenant_id, run_id,
        )
        return _run_of(row) if row else None

    async def get_session_for_worker(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        row = await self._pool.fetchrow(
            """SELECT s.*, w.path AS workspace_path
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.tenant_id = $1 AND s.id = $2 AND s.deleted_at IS NULL""",
            tenant_id, session_id,
        )
        return _session_of(row) if row else None

    # ── workspace sandbox ─────────────────────────────────────────────

    async def get_workspace_sandbox_for_worker(
        self, tenant_id: str, session_id: str
    ) -> WorkspaceSandboxRecord | None:
        row = await self._pool.fetchrow(
            """SELECT w.id AS workspace_id, w.sandbox_id, w.sandbox_provider
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.tenant_id = $1 AND s.id = $2""",
            tenant_id, session_id,
        )
        if not row:
            return None
        return WorkspaceSandboxRecord(
            workspace_id=str(row["workspace_id"]),
            sandbox_id=str(row["sandbox_id"]) if row["sandbox_id"] else None,
            sandbox_provider=str(row["sandbox_provider"]),
        )

    async def get_workspace_id_by_external_key(self, external_key: str) -> str | None:
        row = await self._pool.fetchrow(
            """SELECT w.id AS workspace_id
               FROM agent_sessions s
               JOIN workspaces w ON w.id = s.workspace_id
               WHERE s.external_key = $1""",
            external_key,
        )
        return str(row["workspace_id"]) if row else None

    async def save_workspace_sandbox_id(
        self, tenant_id: str, workspace_id: str, sandbox_id: str
    ) -> bool:
        result = await self._pool.execute(
            """UPDATE workspaces
               SET sandbox_id = $3, sandbox_provider = 'docker'
               WHERE tenant_id = $1 AND id = $2 AND sandbox_id IS NULL""",
            tenant_id, workspace_id, sandbox_id,
        )
        return result == "UPDATE 1"

    async def clear_workspace_sandbox_id(
        self, tenant_id: str, workspace_id: str, sandbox_id: str
    ) -> None:
        await self._pool.execute(
            "UPDATE workspaces SET sandbox_id = NULL WHERE tenant_id = $1 AND id = $2 AND sandbox_id = $3",
            tenant_id, workspace_id, sandbox_id,
        )

    # ── run status / events ───────────────────────────────────────────

    async def update_run_status(
        self, tenant_id: str, run_id: str, status: str, error: dict[str, str] | None = None
    ) -> None:
        terminal = status in ("completed", "failed", "cancelled")
        result = await self._pool.execute(
            """UPDATE agent_runs
               SET status = $3,
                   started_at = CASE WHEN $3 = 'running' THEN COALESCE(started_at, now()) ELSE started_at END,
                   finished_at = CASE WHEN $4 THEN now() ELSE finished_at END,
                   error_code = $5,
                   error_message = $6,
                   updated_at = now()
               WHERE tenant_id = $1 AND id = $2""",
            tenant_id, run_id, status, terminal,
            error.get("code") if error else None,
            error.get("message") if error else None,
        )
        if result != "UPDATE 1":
            raise RepositoryNotFoundError("run")

    async def try_mark_run_running(self, tenant_id: str, run_id: str) -> bool:
        result = await self._pool.execute(
            """UPDATE agent_runs
               SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
               WHERE tenant_id = $1 AND id = $2
                 AND status IN ('queued', 'running', 'waiting_approval', 'waiting_question')
                 AND cancel_requested_at IS NULL""",
            tenant_id, run_id,
        )
        return result == "UPDATE 1"

    async def append_event(
        self, tenant_id: str, event: AgentEvent
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                sequence = await conn.fetchrow(
                    """UPDATE agent_runs
                       SET last_event_seq = last_event_seq + 1, updated_at = now()
                       WHERE tenant_id = $1 AND id = $2
                       RETURNING last_event_seq""",
                    tenant_id, event.run_id,
                )
                if not sequence:
                    raise RepositoryNotFoundError("run")
                seq = int(sequence["last_event_seq"])
                await conn.execute(
                    """INSERT INTO run_events (run_id, tenant_id, seq, event_type, payload)
                       VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    event.run_id, tenant_id, seq, event.type,
                    event.model_dump_json(by_alias=True),
                )
                await conn.execute("COMMIT")
                payload = event.model_dump(by_alias=True)
                payload["seq"] = seq
                return payload
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def list_events(
        self, context: AuthContext, run_id: str, after_seq: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """SELECT e.seq, e.payload
               FROM run_events e
               LEFT JOIN interrupts i
                 ON i.tenant_id = e.tenant_id
                 AND i.run_id = e.run_id
                 AND i.id = e.payload->>'interruptId'
               WHERE e.tenant_id = $1 AND e.run_id = $2 AND e.seq > $3
                 AND NOT (
                   e.event_type IN ('approval.required', 'question.required')
                   AND i.status IN ('resolved', 'cancelled')
                 )
               ORDER BY e.seq ASC
               LIMIT $4""",
            context.tenant_id, run_id, after_seq, limit,
        )
        return [{**r["payload"], "seq": r["seq"]} for r in rows]

    async def list_events_for_worker(
        self, tenant_id: str, run_id: str, limit: int = 2000
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        rows = await self._pool.fetch(
            """SELECT seq, payload FROM run_events
               WHERE tenant_id = $1 AND run_id = $2
               ORDER BY seq ASC LIMIT $3""",
            tenant_id, run_id, limit,
        )
        return [{**r["payload"], "seq": r["seq"]} for r in rows]

    # ── interrupts ────────────────────────────────────────────────────

    async def create_interrupt(
        self, tenant_id: str, run_id: str, interrupt: dict[str, Any]
    ) -> None:
        await self._pool.execute(
            """INSERT INTO interrupts (id, tenant_id, run_id, kind, request)
               VALUES ($1, $2, $3, $4, $5::jsonb)
               ON CONFLICT (run_id, id) DO NOTHING""",
            interrupt["id"], tenant_id, run_id, interrupt["kind"],
            json_dumps(interrupt["request"]),
        )

    async def resolve_interrupt(
        self, context: AuthContext, run_id: str, interrupt_id: str,
        kind: str, response: Any, observability_context: ObservabilityContext | None = None
    ) -> InterruptRecord | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                run = await conn.fetchrow(
                    """SELECT r.user_id, r.session_id, r.knowledge_base_ids, w.path AS workspace_path,
                              s.approval_mode
                       FROM agent_runs r
                       JOIN agent_sessions s ON s.id = r.session_id
                       JOIN workspaces w ON w.id = s.workspace_id
                       WHERE r.tenant_id = $1 AND r.user_id = $2 AND r.id = $3
                         AND s.deleted_at IS NULL
                       FOR UPDATE OF s""",
                    context.tenant_id, context.user_id, run_id,
                )
                if not run:
                    raise RepositoryNotFoundError("run")

                result = await conn.fetchrow(
                    """UPDATE interrupts
                       SET status = 'resolved', response = $5::jsonb, resolved_at = now()
                       WHERE tenant_id = $1 AND run_id = $2 AND id = $3 AND kind = $4
                         AND status = 'pending'
                       RETURNING *""",
                    context.tenant_id, run_id, interrupt_id, kind,
                    json_dumps(response),
                )
                if not result:
                    await conn.execute("COMMIT")
                    return None

                approval_decision = response if kind == "approval" else None
                grants_session = (
                    approval_decision
                    and approval_decision.get("decision") == "approve"
                    and approval_decision.get("scope") == "session"
                )
                if grants_session:
                    await conn.execute(
                        """UPDATE agent_sessions
                           SET approval_mode = 'session', updated_at = now()
                           WHERE tenant_id = $1 AND user_id = $2 AND id = $3""",
                        context.tenant_id, context.user_id, str(run["session_id"]),
                    )

                approval_mode = "session" if grants_session else str(run["approval_mode"])
                common = {
                    "tenantId": context.tenant_id,
                    "userId": str(run["user_id"]),
                    "sessionId": str(run["session_id"]),
                    "runId": run_id,
                    "workspacePath": str(run["workspace_path"]),
                    "approvalMode": approval_mode,
                    "knowledgeBaseIds": [str(x) for x in run["knowledge_base_ids"]] if run["knowledge_base_ids"] else [],
                }
                if observability_context:
                    common["observability"] = observability_context.model_dump(by_alias=True)

                if kind == "approval":
                    job_payload = {**common, "kind": "resume-approval", "decision": response}
                else:
                    job_payload = {**common, "kind": "resume-question", "answer": response}

                outbox_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO run_dispatch_outbox (id, tenant_id, run_id, job_kind, payload)
                       VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    outbox_id, context.tenant_id, run_id,
                    job_payload["kind"], json_dumps(job_payload),
                )
                await conn.execute("COMMIT")
                return InterruptRecord(
                    id=str(result["id"]),
                    run_id=str(result["run_id"]),
                    kind=str(result["kind"]),
                    request=result["request"],
                    response=result["response"],
                )
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    # ── cancellation ──────────────────────────────────────────────────

    async def request_cancellation(
        self, context: AuthContext, run_id: str
    ) -> CancellationResult | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                selected = await conn.fetchrow(
                    """SELECT * FROM agent_runs
                       WHERE tenant_id = $1 AND id = $2
                         AND status IN ('queued', 'running', 'waiting_approval', 'waiting_question')
                       FOR UPDATE""",
                    context.tenant_id, run_id,
                )
                current = selected
                if not current:
                    await conn.execute("COMMIT")
                    return None

                if current["status"] == "running":
                    updated = await conn.fetchrow(
                        """UPDATE agent_runs
                           SET cancel_requested_at = now(), updated_at = now()
                           WHERE tenant_id = $1 AND id = $2
                           RETURNING *""",
                        context.tenant_id, run_id,
                    )
                    await conn.execute("COMMIT")
                    return CancellationResult(run=_run_of(updated), event=None)

                seq = int(current["last_event_seq"]) + 1
                event = {
                    "runId": run_id,
                    "seq": seq,
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "type": "run.cancelled",
                }
                updated = await conn.fetchrow(
                    """UPDATE agent_runs
                       SET status = 'cancelled', cancel_requested_at = now(),
                           last_event_seq = $3, finished_at = now(), updated_at = now()
                       WHERE tenant_id = $1 AND id = $2
                       RETURNING *""",
                    context.tenant_id, run_id, seq,
                )
                await conn.execute(
                    """INSERT INTO run_events (run_id, tenant_id, seq, event_type, payload)
                       VALUES ($1, $2, $3, 'run.cancelled', $4::jsonb)""",
                    run_id, context.tenant_id, seq, json_dumps(event),
                )
                await conn.execute(
                    """UPDATE interrupts
                       SET status = 'cancelled', resolved_at = now()
                       WHERE tenant_id = $1 AND run_id = $2 AND status = 'pending'""",
                    context.tenant_id, run_id,
                )
                await conn.execute(
                    """UPDATE run_dispatch_outbox
                       SET published_at = now(), locked_at = NULL,
                           last_error = 'cancelled before dispatch'
                       WHERE tenant_id = $1 AND run_id = $2 AND published_at IS NULL""",
                    context.tenant_id, run_id,
                )
                await conn.execute("COMMIT")
                return CancellationResult(run=_run_of(updated), event=event)
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    # ── outbox ────────────────────────────────────────────────────────

    async def claim_dispatches(self, limit: int, lease_ms: int) -> list[DispatchOutboxRecord]:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                rows = await conn.fetch(
                    """WITH candidates AS (
                         SELECT id
                         FROM run_dispatch_outbox
                         WHERE published_at IS NULL
                           AND available_at <= now()
                           AND (
                             locked_at IS NULL
                             OR locked_at < now() - ($2::integer * interval '1 millisecond')
                           )
                         ORDER BY created_at ASC
                         FOR UPDATE SKIP LOCKED
                         LIMIT $1
                       )
                       UPDATE run_dispatch_outbox AS dispatch
                       SET locked_at = now(), attempts = dispatch.attempts + 1
                       FROM candidates
                       WHERE dispatch.id = candidates.id
                       RETURNING dispatch.*""",
                    limit, lease_ms,
                )
                await conn.execute("COMMIT")
                return [
                    DispatchOutboxRecord(
                        id=str(r["id"]),
                        tenant_id=str(r["tenant_id"]),
                        run_id=str(r["run_id"]),
                        job=r["payload"],
                        attempts=int(r["attempts"]),
                    )
                    for r in rows
                ]
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def mark_dispatch_published(self, id: str) -> None:
        await self._pool.execute(
            """UPDATE run_dispatch_outbox
               SET published_at = now(), locked_at = NULL, last_error = NULL
               WHERE id = $1""",
            id,
        )

    async def mark_dispatch_consumed(self, id: str) -> None:
        await self._pool.execute(
            """UPDATE run_dispatch_outbox
               SET consumed_at = COALESCE(consumed_at, now())
               WHERE id = $1""",
            id,
        )

    async def requeue_stale_dispatches(self, stale_ms: int, limit: int) -> int:
        rows = await self._pool.fetch(
            """WITH candidates AS (
                 SELECT dispatch.id
                 FROM run_dispatch_outbox dispatch
                 JOIN agent_runs run ON run.id = dispatch.run_id
                 WHERE dispatch.published_at IS NOT NULL
                   AND dispatch.consumed_at IS NULL
                   AND dispatch.published_at < now() - ($1::integer * interval '1 millisecond')
                   AND run.cancel_requested_at IS NULL
                   AND (
                     (dispatch.job_kind = 'start' AND run.status = 'queued')
                     OR (dispatch.job_kind = 'resume-approval' AND run.status = 'waiting_approval')
                     OR (dispatch.job_kind = 'resume-question' AND run.status = 'waiting_question')
                   )
                 ORDER BY dispatch.published_at ASC
                 FOR UPDATE OF dispatch SKIP LOCKED
                 LIMIT $2
               )
               UPDATE run_dispatch_outbox AS dispatch
               SET published_at = NULL, locked_at = NULL, available_at = now(),
                   last_error = 'requeued by stale dispatch reconciler'
               FROM candidates
               WHERE dispatch.id = candidates.id
               RETURNING dispatch.id""",
            stale_ms, limit,
        )
        return len(rows)

    async def reschedule_dispatch(self, id: str, error: str, delay_ms: int) -> None:
        await self._pool.execute(
            """UPDATE run_dispatch_outbox
               SET locked_at = NULL,
                   available_at = now() + ($3::integer * interval '1 millisecond'),
                   last_error = left($2, 4000)
               WHERE id = $1 AND published_at IS NULL""",
            id, error, delay_ms,
        )

    # ── artifacts ─────────────────────────────────────────────────────

    async def create_artifact(
        self, context: AuthContext, input: dict[str, Any]
    ) -> ArtifactRecord:
        row = await self._pool.fetchrow(
            """INSERT INTO artifacts
               (id, tenant_id, run_id, name, object_key, content_type, size_bytes, sha256)
               SELECT $1, $2, r.id, $4, $5, $6, $7, $8
               FROM agent_runs r
               WHERE r.tenant_id = $2 AND r.id = $3
               RETURNING *""",
            input["id"], context.tenant_id, input["run_id"],
            input["name"], input["object_key"], input["content_type"],
            input["size_bytes"], input["sha256"],
        )
        if not row:
            raise RepositoryNotFoundError("run")
        return _artifact_of(row)

    async def get_artifact(self, context: AuthContext, artifact_id: str) -> ArtifactRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM artifacts WHERE tenant_id = $1 AND id = $2",
            context.tenant_id, artifact_id,
        )
        return _artifact_of(row) if row else None

    async def mark_artifact_ready(
        self, context: AuthContext, artifact_id: str
    ) -> ArtifactRecord | None:
        row = await self._pool.fetchrow(
            """UPDATE artifacts
               SET status = 'ready', uploaded_at = COALESCE(uploaded_at, now())
               WHERE tenant_id = $1 AND id = $2 AND status = 'pending'
               RETURNING *""",
            context.tenant_id, artifact_id,
        )
        if row:
            return _artifact_of(row)
        return await self.get_artifact(context, artifact_id)

    # ── chat attachments ──────────────────────────────────────────────

    async def create_chat_attachment(
        self, context: AuthContext, input: dict[str, Any]
    ) -> ChatAttachmentRecord:
        row = await self._pool.fetchrow(
            """INSERT INTO chat_attachments
               (id, tenant_id, user_id, object_key, filename, content_type,
                size_bytes, sha256, content_sha256, content_encoding, upload_id, kind)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
               RETURNING *""",
            input["id"], context.tenant_id, context.user_id,
            input["object_key"], input["filename"], input["content_type"],
            input["size_bytes"], input["sha256"],
            input.get("content_sha256", input["sha256"]),
            input.get("content_encoding"),
            input.get("upload_id"),
            input["kind"],
        )
        return _chat_attachment_of(row)

    async def find_ready_chat_attachment_by_content_hash(
        self, tenant_id: str, content_sha256: str
    ) -> ChatAttachmentRecord | None:
        row = await self._pool.fetchrow(
            """SELECT * FROM chat_attachments
               WHERE tenant_id = $1 AND content_sha256 = $2
                 AND status = 'ready' AND run_id IS NOT NULL
               ORDER BY uploaded_at DESC
               LIMIT 1""",
            tenant_id, content_sha256,
        )
        return _chat_attachment_of(row) if row else None

    async def set_chat_attachment_upload_id(
        self, attachment_id: str, upload_id: str | None
    ) -> None:
        await self._pool.execute(
            "UPDATE chat_attachments SET upload_id = $2 WHERE id = $1",
            attachment_id, upload_id,
        )

    async def count_chat_attachments_by_object_key(
        self, object_key: str, exclude_id: str | None = None
    ) -> int:
        row = await self._pool.fetchrow(
            """SELECT COUNT(*)::int AS count FROM chat_attachments
               WHERE object_key = $1
                 AND ($2::uuid IS NULL OR id <> $2)""",
            object_key, exclude_id,
        )
        return int(row["count"]) if row else 0

    async def get_chat_attachment(
        self, context: AuthContext, attachment_id: str
    ) -> ChatAttachmentRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM chat_attachments WHERE tenant_id = $1 AND user_id = $2 AND id = $3",
            context.tenant_id, context.user_id, attachment_id,
        )
        return _chat_attachment_of(row) if row else None

    async def get_ready_chat_attachments(
        self, context: AuthContext, attachment_ids: list[str]
    ) -> list[ChatAttachmentRecord]:
        if not attachment_ids:
            return []
        rows = await self._pool.fetch(
            """SELECT * FROM chat_attachments
               WHERE tenant_id = $1 AND user_id = $2 AND status = 'ready'
                 AND id = ANY($3::uuid[])""",
            context.tenant_id, context.user_id, attachment_ids,
        )
        by_id = {str(r["id"]): _chat_attachment_of(r) for r in rows}
        return [by_id[i] for i in attachment_ids if i in by_id]

    async def mark_chat_attachment_ready(
        self, context: AuthContext, attachment_id: str
    ) -> ChatAttachmentRecord | None:
        row = await self._pool.fetchrow(
            """UPDATE chat_attachments
               SET status = 'ready', uploaded_at = COALESCE(uploaded_at, now())
               WHERE tenant_id = $1 AND user_id = $2 AND id = $3 AND status = 'pending'
               RETURNING *""",
            context.tenant_id, context.user_id, attachment_id,
        )
        if row:
            return _chat_attachment_of(row)
        return await self.get_chat_attachment(context, attachment_id)

    async def list_chat_attachments_by_runs(
        self, context: AuthContext, run_ids: list[str]
    ) -> dict[str, list[ChatAttachmentRecord]]:
        result: dict[str, list[ChatAttachmentRecord]] = {}
        if not run_ids:
            return result
        rows = await self._pool.fetch(
            """SELECT * FROM chat_attachments
               WHERE tenant_id = $1 AND run_id = ANY($2::uuid[])
               ORDER BY created_at ASC""",
            context.tenant_id, run_ids,
        )
        for r in rows:
            attachment = _chat_attachment_of(r)
            if not attachment.run_id:
                continue
            result.setdefault(attachment.run_id, []).append(attachment)
        return result

    async def list_stale_unlinked_attachments(
        self, cutoff: datetime, limit: int
    ) -> list[dict[str, str]]:
        rows = await self._pool.fetch(
            """SELECT id, object_key FROM chat_attachments
               WHERE run_id IS NULL AND created_at < $1
               ORDER BY created_at ASC
               LIMIT $2""",
            cutoff, limit,
        )
        return [{"id": str(r["id"]), "object_key": str(r["object_key"])} for r in rows]

    async def delete_chat_attachment(self, attachment_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM chat_attachments WHERE id = $1", attachment_id,
        )

    # ── admin / KB grants ─────────────────────────────────────────────

    async def list_knowledge_base_grants(self, tenant_id: str, user_id: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT kb_id FROM knowledge_base_grants WHERE tenant_id = $1 AND user_id = $2",
            tenant_id, user_id,
        )
        return [str(r["kb_id"]) for r in rows]

    async def replace_knowledge_base_grants(
        self, tenant_id: str, user_id: str, kb_ids: list[str], granted_by: str
    ) -> list[str]:
        unique = list(dict.fromkeys(kb_ids))
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                membership = await conn.fetchrow(
                    "SELECT 1 FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                if not membership:
                    raise RepositoryNotFoundError("user")
                if unique:
                    known = await conn.fetch(
                        """SELECT id FROM knowledge_bases
                           WHERE tenant_id = $1 AND deleted_at IS NULL AND id = ANY($2::uuid[])""",
                        tenant_id, unique,
                    )
                    if len(known) != len(unique):
                        raise RepositoryNotFoundError("knowledge_base")
                await conn.execute(
                    "DELETE FROM knowledge_base_grants WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id, user_id,
                )
                for kb_id in unique:
                    await conn.execute(
                        """INSERT INTO knowledge_base_grants
                           (id, tenant_id, kb_id, user_id, granted_by_user_id)
                           VALUES ($1, $2, $3, $4, $5)
                           ON CONFLICT (kb_id, user_id) DO NOTHING""",
                        uuid.uuid4(), tenant_id, kb_id, user_id, granted_by,
                    )
                await conn.execute("COMMIT")
                return unique
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    # ── orphan cleanup ────────────────────────────────────────────────

    async def fail_orphan_runs_before(self, cutoff: datetime) -> int:
        result = await self._pool.execute(
            """UPDATE agent_runs
               SET status = 'failed',
                   error_code = 'worker_restarted',
                   error_message = 'Worker 重启导致任务中断，请重新发送',
                   updated_at = now()
               WHERE status IN ('running', 'waiting_approval', 'waiting_question')
                 AND updated_at < $1""",
            cutoff,
        )
        # asyncpg execute returns "UPDATE n"
        try:
            return int(result.split(" ")[1])
        except (IndexError, ValueError):
            return 0
