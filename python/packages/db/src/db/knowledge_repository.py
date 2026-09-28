"""KnowledgeRepository — raw asyncpg SQL port of packages/db/src/knowledge-repository.ts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg
from contracts import AuthContext

from ._json import json_dumps
from .errors import ForbiddenKnowledgeError

_MAX_CITATIONS = 50


def _bounded_citations(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [
        {k: v for k, v in item.items() if k != "passage"}
        if isinstance(item, dict)
        else item
        for item in value[:_MAX_CITATIONS]
    ]


class KnowledgeEmbeddingProfile:
    def __init__(
        self,
        key: str,
        model: str,
        dimension: int,
        collection_name: str,
    ) -> None:
        self.key = key
        self.model = model
        self.dimension = dimension
        self.collection_name = collection_name


class KnowledgeRepositoryOptions:
    def __init__(self, embedding_profile: KnowledgeEmbeddingProfile | None = None) -> None:
        self.embedding_profile = embedding_profile


class KnowledgeRepository:
    def __init__(
        self,
        pool: asyncpg.Pool,
        options: KnowledgeRepositoryOptions | None = None,
    ) -> None:
        self._pool = pool
        self._options = options or KnowledgeRepositoryOptions()
        self._leases: dict[str, str] = {}
        self._caption_leases: dict[str, str] = {}

    # ── ACL helpers ───────────────────────────────────────────────────

    @staticmethod
    def _readable(target: str, me_param: int, tenant_param: int, roles_param: int) -> str:
        return (
            f"(visibility = 'tenant' OR owner_user_id = ${me_param} "
            f"OR EXISTS (SELECT 1 FROM knowledge_base_grants g "
            f"WHERE g.kb_id = {target} AND g.user_id = ${me_param} AND g.tenant_id = ${tenant_param}) "
            f"OR ${roles_param}::text[] && ARRAY['admin']::text[])"
        )

    @staticmethod
    def _writable(me_param: int, roles_param: int) -> str:
        return f"(owner_user_id = ${me_param} OR ${roles_param}::text[] && ARRAY['admin']::text[])"

    # ── knowledge bases ───────────────────────────────────────────────

    async def list_knowledge_bases(self, auth: AuthContext) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""SELECT k.*,
                (SELECT count(*) FROM knowledge_documents d WHERE d.kb_id = k.id AND d.deleted_at IS NULL)::int AS document_count,
                (SELECT count(*) FROM knowledge_chunks c WHERE c.kb_id = k.id)::int AS chunk_count
               FROM knowledge_bases k
               WHERE k.tenant_id = $1 AND k.deleted_at IS NULL AND {self._readable('k.id', 2, 1, 3)}
               ORDER BY k.updated_at DESC, k.created_at DESC""",
            auth.tenant_id, auth.user_id, auth.roles,
        )
        return [dict(r) for r in rows]

    async def get_knowledge_base(self, auth: AuthContext, id: str) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""SELECT * FROM knowledge_bases
               WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
                 AND {self._readable('knowledge_bases.id', 3, 1, 4)}""",
            auth.tenant_id, id, auth.user_id, auth.roles,
        )
        return dict(row) if row else None

    async def can_write_knowledge_base(self, auth: AuthContext, id: str) -> bool:
        row = await self._pool.fetchrow(
            f"""SELECT 1 FROM knowledge_bases
               WHERE tenant_id=$1 AND id=$2 AND deleted_at IS NULL
                 AND {self._writable(3, 4)}""",
            auth.tenant_id, id, auth.user_id, auth.roles,
        )
        return row is not None

    async def create_knowledge_base(self, auth: AuthContext, input: dict[str, Any]) -> dict[str, Any]:
        if "owner" not in auth.roles and "admin" not in auth.roles:
            raise ForbiddenKnowledgeError()
        profile = input.get("embedding_profile") or (
            self._options.embedding_profile.model_dump() if self._options.embedding_profile else None
        )
        if not profile:
            raise ValueError("Knowledge embedding profile is required")
        row = await self._pool.fetchrow(
            """INSERT INTO knowledge_bases
               (id, tenant_id, owner_user_id, name, description, visibility,
                embedding_profile_key, embedding_model, embedding_dim,
                collection_name, chunk_size, chunk_overlap, top_k, max_hops,
                graph_enabled, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,800,100,10,2,true,'ready')
               RETURNING *""",
            input.get("id", uuid.uuid4()),
            auth.tenant_id,
            auth.user_id,
            input["name"],
            input.get("description"),
            input.get("visibility", "private"),
            profile["key"],
            profile["model"],
            profile["dimension"],
            profile["collection_name"],
        )
        return dict(row)

    async def update_knowledge_base(
        self,
        auth: AuthContext,
        id: str,
        input: dict[str, Any],
    ) -> dict[str, Any] | None:
        existing = await self._pool.fetchrow(
            f"""SELECT * FROM knowledge_bases
               WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
                 AND {self._writable(3, 4)}""",
            auth.tenant_id, id, auth.user_id, auth.roles,
        )
        if not existing:
            return None
        can_change_visibility = (
            str(existing["owner_user_id"]) == str(auth.user_id)
            or "admin" in auth.roles
        )
        visibility = input.get("visibility") if can_change_visibility else None
        row = await self._pool.fetchrow(
            """UPDATE knowledge_bases SET
                 name = COALESCE($3, name),
                 description = CASE WHEN $4::boolean THEN $5 ELSE description END,
                 visibility = COALESCE($6, visibility),
                 updated_at = now()
               WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
               RETURNING *""",
            auth.tenant_id,
            id,
            input.get("name"),
            input.get("description") is not None,
            input.get("description"),
            visibility,
        )
        return dict(row) if row else None

    async def delete_knowledge_base(self, auth: AuthContext, id: str) -> bool:
        result = await self._pool.execute(
            f"""UPDATE knowledge_bases SET deleted_at = now(), updated_at = now()
               WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
                 AND {self._writable(3, 4)}""",
            auth.tenant_id, id, auth.user_id, auth.roles,
        )
        return result == "UPDATE 1"

    # ── knowledge documents ───────────────────────────────────────────

    async def list_knowledge_documents(self, auth: AuthContext, kb_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""SELECT d.* FROM knowledge_documents d
               JOIN knowledge_bases k ON k.id = d.kb_id
               WHERE d.tenant_id = $1 AND d.kb_id = $2 AND d.deleted_at IS NULL
                 AND k.deleted_at IS NULL AND {self._readable('k.id', 3, 1, 4)}
               ORDER BY d.created_at DESC""",
            auth.tenant_id, kb_id, auth.user_id, auth.roles,
        )
        return [dict(r) for r in rows]

    async def get_knowledge_document(
        self, auth: AuthContext, kb_id: str, id: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""SELECT d.* FROM knowledge_documents d
               JOIN knowledge_bases k ON k.id = d.kb_id
               WHERE d.tenant_id = $1 AND d.kb_id = $2 AND d.id = $3
                 AND d.deleted_at IS NULL AND k.deleted_at IS NULL
                 AND {self._readable('k.id', 4, 1, 5)}""",
            auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
        )
        return dict(row) if row else None

    async def rename_knowledge_document(
        self, auth: AuthContext, kb_id: str, id: str, name: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""UPDATE knowledge_documents d SET name = $4, updated_at = now()
               FROM knowledge_bases k
               WHERE d.tenant_id = $1 AND d.kb_id = $2 AND d.id = $3 AND d.deleted_at IS NULL
                 AND k.id = d.kb_id AND k.deleted_at IS NULL AND {self._writable(5, 6)}
               RETURNING d.*""",
            auth.tenant_id, kb_id, id, name, auth.user_id, auth.roles,
        )
        return dict(row) if row else None

    async def list_document_chunks(
        self,
        auth: AuthContext,
        kb_id: str,
        document_id: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        opts = options or {}
        limit = min(200, max(1, opts.get("limit", 100)))
        offset = max(0, opts.get("offset", 0))
        search = opts.get("search", "")
        has_search = bool(search and search.strip())
        search_term = search.strip() if has_search else None

        rows = await self._pool.fetch(
            f"""SELECT c.id, c.kb_id, c.document_id, c.ordinal, c.text, c.token_count,
                      c.heading, c.metadata, c.created_at, d.name AS document_name
               FROM knowledge_chunks c
               JOIN knowledge_bases k ON k.id = c.kb_id
               JOIN knowledge_documents d ON d.id = c.document_id
               WHERE c.tenant_id = $1 AND c.kb_id = $2 AND c.document_id = $3
                 AND k.deleted_at IS NULL AND d.deleted_at IS NULL
                 AND {self._readable('k.id', 4, 1, 5)}
                 AND ($6::text IS NULL OR c.text ILIKE ('%' || $6 || '%') OR c.id::text ILIKE ('%' || $6 || '%'))
               ORDER BY c.ordinal ASC
               LIMIT $7 OFFSET $8""",
            auth.tenant_id, kb_id, document_id, auth.user_id, auth.roles,
            search_term, limit, offset,
        )
        count_row = await self._pool.fetchrow(
            f"""SELECT count(*)::int AS total
               FROM knowledge_chunks c
               JOIN knowledge_bases k ON k.id = c.kb_id
               JOIN knowledge_documents d ON d.id = c.document_id
               WHERE c.tenant_id = $1 AND c.kb_id = $2 AND c.document_id = $3
                 AND k.deleted_at IS NULL AND d.deleted_at IS NULL
                 AND {self._readable('k.id', 4, 1, 5)}
                 AND ($6::text IS NULL OR c.text ILIKE ('%' || $6 || '%') OR c.id::text ILIKE ('%' || $6 || '%'))""",
            auth.tenant_id, kb_id, document_id, auth.user_id, auth.roles,
            search_term,
        )
        return {"rows": [dict(r) for r in rows], "total": count_row["total"] if count_row else 0}

    async def create_document_upload(
        self, auth: AuthContext, input: dict[str, Any]
    ) -> dict[str, Any] | None:
        kb = await self._pool.fetchrow(
            f"""SELECT id FROM knowledge_bases
               WHERE tenant_id=$1 AND id=$2 AND deleted_at IS NULL AND {self._writable(3, 4)}""",
            auth.tenant_id, input["kb_id"], auth.user_id, auth.roles,
        )
        if not kb:
            return None
        directory = input["directory"] if isinstance(input.get("directory"), str) else ""
        row = await self._pool.fetchrow(
            """INSERT INTO knowledge_documents
               (id, kb_id, tenant_id, name, mime, size_bytes, content_hash, object_key,
                status, directory, stored_size_bytes, stored_sha256, content_encoding)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',$9,$10,$11,$12)
               ON CONFLICT (kb_id, content_hash) WHERE deleted_at IS NULL DO NOTHING
               RETURNING *""",
            input.get("document_id", uuid.uuid4()),
            input["kb_id"],
            auth.tenant_id,
            input["name"],
            input["mime"],
            input["size_bytes"],
            input["sha256"],
            input["object_key"],
            directory,
            input.get("stored_size_bytes"),
            input.get("stored_sha256"),
            input.get("content_encoding"),
        )
        if row:
            return dict(row)
        existing = await self._pool.fetchrow(
            "SELECT * FROM knowledge_documents WHERE kb_id=$1 AND content_hash=$2 AND deleted_at IS NULL LIMIT 1",
            input["kb_id"], input["sha256"],
        )
        return dict(existing) if existing else None

    async def confirm_document_upload(
        self, auth: AuthContext, kb_id: str, id: str, input: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                doc = await conn.fetchrow(
                    """SELECT d.* FROM knowledge_documents d
                       JOIN knowledge_bases k ON k.id=d.kb_id
                       WHERE d.tenant_id=$1 AND d.kb_id=$2 AND d.id=$3 AND d.deleted_at IS NULL
                         AND k.deleted_at IS NULL AND (k.owner_user_id=$4 OR $5::text[] && ARRAY['admin']::text[])
                       FOR UPDATE""",
                    auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
                )
                if not doc:
                    await conn.execute("ROLLBACK")
                    return None
                existing = await conn.fetchrow(
                    """SELECT * FROM knowledge_index_jobs
                       WHERE tenant_id=$1 AND document_id=$2 AND status IN ('queued','running')
                       ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                    auth.tenant_id, id,
                )
                if existing:
                    await conn.execute("COMMIT")
                    return {"document": dict(doc), "job": dict(existing), "created": False}
                updated = await conn.fetchrow(
                    """UPDATE knowledge_documents
                       SET status='queued', stored_size_bytes=$4, stored_sha256=$5,
                           upload_id=NULL, updated_at=now()
                       WHERE tenant_id=$1 AND kb_id=$2 AND id=$3
                       RETURNING *""",
                    auth.tenant_id, kb_id, id, input["size_bytes"], input["sha256"],
                )
                job = await conn.fetchrow(
                    """INSERT INTO knowledge_index_jobs
                       (id, kb_id, tenant_id, document_id, kind, status)
                       VALUES ($1,$2,$3,$4,'index','queued') RETURNING *""",
                    uuid.uuid4(), kb_id, auth.tenant_id, id,
                )
                await conn.execute("COMMIT")
                return {"document": dict(updated), "job": dict(job), "created": True}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def delete_knowledge_document(self, auth: AuthContext, kb_id: str, id: str) -> bool:
        result = await self._pool.execute(
            """UPDATE knowledge_documents d SET deleted_at=now(), updated_at=now()
               FROM knowledge_bases k
               WHERE d.tenant_id=$1 AND d.kb_id=$2 AND d.id=$3 AND k.id=d.kb_id
                 AND (k.owner_user_id=$4 OR $5::text[] && ARRAY['admin']::text[])
                 AND d.deleted_at IS NULL""",
            auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
        )
        return result.startswith("UPDATE")

    # ── index jobs ────────────────────────────────────────────────────

    async def list_queued_or_stale_index_jobs(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Invalid limit")
        rows = await self._pool.fetch(
            """SELECT * FROM knowledge_index_jobs
               WHERE status = 'queued' OR (status = 'running' AND lease_expires_at < $1)
               ORDER BY created_at ASC LIMIT $2""",
            now, limit,
        )
        return [dict(r) for r in rows]

    async def claim_index_job(
        self, tenant_id: str, job_id: str, lease_ms: int
    ) -> dict[str, str] | None:
        lease_token = str(uuid.uuid4())
        row = await self._pool.fetchrow(
            """UPDATE knowledge_index_jobs
               SET status = 'running', error_code = $4, attempts = attempts + 1,
                   lease_expires_at = now() + ($3::bigint * interval '1 millisecond'),
                   started_at = COALESCE(started_at, now()), updated_at = now()
               WHERE tenant_id = $1 AND id = $2
                 AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
               RETURNING id""",
            tenant_id, job_id, lease_ms, lease_token,
        )
        if not row:
            return None
        self._leases[f"{tenant_id}:{job_id}"] = lease_token
        return {"job_id": job_id, "lease_token": lease_token}

    def _owns_lease(self, tenant_id: str, job_id: str, token: str) -> bool:
        return self._leases.get(f"{tenant_id}:{job_id}") == token

    async def mark_index_stage(
        self, tenant_id: str, job_id: str, token: str, stage: str
    ) -> bool:
        if not self._owns_lease(tenant_id, job_id, token):
            return False
        result = await self._pool.execute(
            """UPDATE knowledge_index_jobs
               SET status = $4, updated_at = now()
               WHERE id = $1 AND tenant_id = $2 AND error_code = $3
                 AND lease_expires_at > now()""",
            job_id, tenant_id, token, stage,
        )
        return result == "UPDATE 1"

    async def complete_index_job(
        self, tenant_id: str, job_id: str, token: str, chunk_count: int = 0
    ) -> bool:
        if not self._owns_lease(tenant_id, job_id, token):
            return False
        result = await self._pool.execute(
            """WITH updated AS (
                 UPDATE knowledge_index_jobs
                 SET status = 'completed', progress = 100, finished_at = now(),
                     lease_expires_at = NULL, updated_at = now()
                 WHERE id = $1 AND tenant_id = $2 AND error_code = $3
                   AND lease_expires_at > now()
                 RETURNING document_id
               )
               UPDATE knowledge_documents d
               SET status = 'ready', chunk_count = $4, indexed_at = now(), updated_at = now()
               FROM updated
               WHERE d.id = updated.document_id AND d.tenant_id = $2""",
            job_id, tenant_id, token, chunk_count,
        )
        return result.startswith("UPDATE")

    async def fail_index_job(
        self, tenant_id: str, job_id: str, token: str, error: Exception | str
    ) -> bool:
        if not self._owns_lease(tenant_id, job_id, token):
            return False
        message = str(error)
        result = await self._pool.execute(
            """WITH updated AS (
                 UPDATE knowledge_index_jobs
                 SET status = 'failed', error_code = 'index_failed', error_message = $4,
                     lease_expires_at = NULL, updated_at = now()
                 WHERE id = $1 AND tenant_id = $2 AND error_code = $3
                   AND lease_expires_at > now()
                 RETURNING document_id
               )
               UPDATE knowledge_documents d
               SET status = 'failed', error_code = 'index_failed', error_message = $4,
                   updated_at = now()
               FROM updated
               WHERE d.id = updated.document_id AND d.tenant_id = $2""",
            job_id, tenant_id, token, message,
        )
        return result.startswith("UPDATE")

    async def get_document_for_index(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            """SELECT * FROM knowledge_documents
               WHERE tenant_id = $1 AND kb_id = $2 AND id = $3 AND deleted_at IS NULL""",
            tenant_id, kb_id, document_id,
        )
        return dict(row) if row else None

    async def set_knowledge_document_upload_id(
        self, tenant_id: str, document_id: str, upload_id: str
    ) -> None:
        await self._pool.execute(
            "UPDATE knowledge_documents SET upload_id=$3, updated_at=now() WHERE tenant_id=$1 AND id=$2",
            tenant_id, document_id, upload_id,
        )

    async def set_knowledge_asset_upload_id(
        self, tenant_id: str, asset_id: str, upload_id: str
    ) -> None:
        await self._pool.execute(
            "UPDATE knowledge_assets SET upload_id=$3, updated_at=now() WHERE tenant_id=$1 AND id=$2",
            tenant_id, asset_id, upload_id,
        )

    async def get_knowledge_base_for_index(
        self, tenant_id: str, kb_id: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM knowledge_bases WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL",
            tenant_id, kb_id,
        )
        return dict(row) if row else None

    # ── assets ────────────────────────────────────────────────────────

    async def create_asset_upload(
        self, auth: AuthContext, input: dict[str, Any]
    ) -> dict[str, Any] | None:
        kb = await self._pool.fetchrow(
            f"""SELECT id FROM knowledge_bases
               WHERE tenant_id=$1 AND id=$2 AND deleted_at IS NULL AND {self._writable(3, 4)}""",
            auth.tenant_id, input["kb_id"], auth.user_id, auth.roles,
        )
        if not kb:
            return None
        row = await self._pool.fetchrow(
            """INSERT INTO knowledge_assets
               (id, tenant_id, kb_id, document_id, rel_path, name, mime,
                size_bytes, content_hash, object_key,
                stored_size_bytes, stored_sha256, content_encoding)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (kb_id, content_hash) WHERE deleted_at IS NULL DO NOTHING
               RETURNING *""",
            input.get("asset_id", uuid.uuid4()),
            auth.tenant_id,
            input["kb_id"],
            input.get("document_id"),
            input["rel_path"],
            input["name"],
            input["mime"],
            input["size_bytes"],
            input["sha256"],
            input["object_key"],
            input.get("stored_size_bytes"),
            input.get("stored_sha256"),
            input.get("content_encoding"),
        )
        if row:
            return dict(row)
        existing = await self._pool.fetchrow(
            "SELECT * FROM knowledge_assets WHERE kb_id=$1 AND content_hash=$2 AND deleted_at IS NULL LIMIT 1",
            input["kb_id"], input["sha256"],
        )
        return dict(existing) if existing else None

    async def confirm_asset_upload(
        self, auth: AuthContext, kb_id: str, id: str, input: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                row = await conn.fetchrow(
                    f"""SELECT a.* FROM knowledge_assets a
                       JOIN knowledge_bases k ON k.id = a.kb_id
                       WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.id=$3 AND a.deleted_at IS NULL
                         AND k.deleted_at IS NULL AND {self._writable(4, 5)}
                       FOR UPDATE""",
                    auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
                )
                if not row:
                    await conn.execute("ROLLBACK")
                    return None
                updated = await conn.fetchrow(
                    """UPDATE knowledge_assets
                       SET stored_size_bytes=$4, stored_sha256=$5, uploaded_at=now(),
                           upload_id=NULL,
                           metadata = COALESCE($6::jsonb, metadata),
                           updated_at = now()
                       WHERE tenant_id=$1 AND kb_id=$2 AND id=$3
                       RETURNING *""",
                    auth.tenant_id, kb_id, id,
                    input["size_bytes"], input["sha256"],
                    json_dumps(input["metadata"]) if input.get("metadata") else None,
                )
                await conn.execute("COMMIT")
                return {"asset": dict(updated)}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def get_knowledge_asset(
        self, auth: AuthContext, kb_id: str, id: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""SELECT a.* FROM knowledge_assets a
               JOIN knowledge_bases k ON k.id = a.kb_id
               WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.id=$3 AND a.deleted_at IS NULL
                 AND k.deleted_at IS NULL AND {self._readable('k.id', 4, 1, 5)}""",
            auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
        )
        return dict(row) if row else None

    async def list_knowledge_assets(
        self, auth: AuthContext, kb_id: str, options: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        opts = options or {}
        limit = min(200, max(1, opts.get("limit", 100)))
        offset = max(0, opts.get("offset", 0))
        document_id = opts.get("document_id")
        if document_id:
            rows = await self._pool.fetch(
                f"""SELECT a.* FROM knowledge_assets a
                   JOIN knowledge_bases k ON k.id = a.kb_id
                   WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.document_id=$3 AND a.deleted_at IS NULL
                     AND k.deleted_at IS NULL AND {self._readable('k.id', 4, 1, 5)}
                   ORDER BY a.rel_path ASC LIMIT $6 OFFSET $7""",
                auth.tenant_id, kb_id, document_id, auth.user_id, auth.roles,
                limit, offset,
            )
        else:
            rows = await self._pool.fetch(
                f"""SELECT a.* FROM knowledge_assets a
                   JOIN knowledge_bases k ON k.id = a.kb_id
                   WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.deleted_at IS NULL
                     AND k.deleted_at IS NULL AND {self._readable('k.id', 3, 1, 4)}
                   ORDER BY a.created_at DESC LIMIT $5 OFFSET $6""",
                auth.tenant_id, kb_id, auth.user_id, auth.roles,
                limit, offset,
            )
        return [dict(r) for r in rows]

    async def delete_knowledge_asset(self, auth: AuthContext, kb_id: str, id: str) -> bool:
        result = await self._pool.execute(
            f"""UPDATE knowledge_assets a SET deleted_at=now(), updated_at=now()
               FROM knowledge_bases k
               WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.id=$3 AND a.deleted_at IS NULL
                 AND k.id = a.kb_id AND k.deleted_at IS NULL AND {self._writable(4, 5)}""",
            auth.tenant_id, kb_id, id, auth.user_id, auth.roles,
        )
        return result.startswith("UPDATE")

    async def list_assets_by_refs(
        self, auth: AuthContext, kb_id: str, refs: list[dict[str, str]]
    ) -> dict[str, dict[str, Any]]:
        if not refs:
            return {}
        doc_ids = [r["document_id"] for r in refs]
        rel_paths = [r["rel_path"] for r in refs]
        rows = await self._pool.fetch(
            """SELECT a.id, a.document_id, a.rel_path, a.name, a.mime, a.caption
               FROM knowledge_assets a
               WHERE a.tenant_id=$1 AND a.kb_id=$2 AND a.deleted_at IS NULL
                 AND (a.document_id, a.rel_path) IN (SELECT * FROM UNNEST($3::uuid[], $4::text[]))""",
            auth.tenant_id, kb_id, doc_ids, rel_paths,
        )
        return {
            f"{r['document_id']}::{r['rel_path']}": dict(r)
            for r in rows
        }

    async def attach_assets_to_document(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> None:
        updated = await self._pool.fetch(
            """UPDATE knowledge_assets a
               SET document_id = $3, updated_at = now()
               FROM knowledge_documents d
               WHERE a.tenant_id = $1 AND a.kb_id = $2 AND a.document_id IS NULL
                 AND a.deleted_at IS NULL
                 AND d.id = $3 AND d.tenant_id = a.tenant_id AND d.kb_id = a.kb_id
                 AND d.deleted_at IS NULL
                 AND d.directory <> ''
                 AND a.rel_path = d.directory || '/' || a.name
               RETURNING a.id""",
            tenant_id, kb_id, document_id,
        )
        if not updated:
            return
        ready = await self._pool.fetchrow(
            """SELECT 1 FROM knowledge_assets
               WHERE tenant_id = $1 AND kb_id = $2 AND document_id = $3
                 AND caption_status = 'ready' LIMIT 1""",
            tenant_id, kb_id, document_id,
        )
        if not ready:
            return
        await self.enqueue_reindex_if_attached(tenant_id, kb_id, document_id, "attach_with_ready_caption")

    async def replace_document_graph(
        self, tenant_id: str, kb_id: str, document_id: str, graph: dict[str, Any]
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                await conn.execute(
                    "DELETE FROM graph_relationships WHERE tenant_id = $1 AND kb_id = $2 AND document_id = $3",
                    tenant_id, kb_id, document_id,
                )
                await conn.execute(
                    "DELETE FROM graph_entities WHERE tenant_id = $1 AND kb_id = $2 AND document_id = $3",
                    tenant_id, kb_id, document_id,
                )
                for entity in graph.get("entities", []):
                    key = entity.get("key", entity.get("name", "").lower())
                    await conn.execute(
                        """INSERT INTO graph_entities
                           (id, tenant_id, kb_id, document_id, entity_key, name, type, chunk_ids)
                           VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT (kb_id, document_id, entity_key)
                           DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type""",
                        tenant_id, kb_id, document_id, key,
                        entity["name"], entity.get("type", "entity"),
                        entity.get("chunk_ids", []),
                    )
                for rel in graph.get("relationships", []):
                    await conn.execute(
                        """INSERT INTO graph_relationships
                           (id, tenant_id, kb_id, document_id, source_key, target_key, relation, description, chunk_ids)
                           VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8)""",
                        tenant_id, kb_id, document_id,
                        rel.get("source_key", rel.get("source")),
                        rel.get("target_key", rel.get("target")),
                        rel.get("relation", rel.get("type")),
                        rel.get("description"), rel.get("chunk_ids", []),
                    )
                await conn.execute("COMMIT")
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def replace_document_chunks(
        self, tenant_id: str, kb_id: str, document_id: str, chunks: list[dict[str, Any]]
    ) -> None:
        await self._pool.execute(
            "DELETE FROM knowledge_chunks WHERE tenant_id = $1 AND kb_id = $2 AND document_id = $3",
            tenant_id, kb_id, document_id,
        )
        for chunk in chunks:
            await self._pool.execute(
                """INSERT INTO knowledge_chunks
                   (id, tenant_id, kb_id, document_id, ordinal, text, token_count, heading, metadata, vector_point_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                   ON CONFLICT (document_id, ordinal)
                   DO UPDATE SET text = EXCLUDED.text, token_count = EXCLUDED.token_count,
                                 metadata = EXCLUDED.metadata, vector_point_id = EXCLUDED.vector_point_id""",
                chunk.get("id", uuid.uuid4()),
                tenant_id, kb_id, document_id,
                chunk["ordinal"], chunk["text"],
                chunk.get("token_count", len(chunk["text"])),
                chunk.get("heading"),
                json_dumps(chunk.get("metadata", {})),
                chunk.get("vector_point_id", chunk.get("id", uuid.uuid4())),
            )

    async def append_retrieval_log(self, input: dict[str, Any]) -> None:
        citations = _bounded_citations(input.get("citations"))
        await self._pool.execute(
            """INSERT INTO knowledge_retrieval_logs
               (id, retrieval_id, tenant_id, user_id, session_id, run_id,
                kb_ids, query, top_k, max_hops, result_count, rerank_status,
                citations, latency_ms, status)
               VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)""",
            input["retrieval_id"], input["tenant_id"], input["user_id"],
            input["session_id"], input["run_id"], input["kb_ids"],
            input["query"], input["top_k"], input["max_hops"],
            input["result_count"], input["rerank_status"],
            json_dumps(citations), input["latency_ms"], input["status"],
        )

    # ── caption jobs ──────────────────────────────────────────────────

    async def enqueue_caption_job(
        self, input: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                asset = await conn.fetchrow(
                    """SELECT id, caption_status FROM knowledge_assets
                       WHERE tenant_id=$1 AND kb_id=$2 AND id=$3 AND deleted_at IS NULL FOR UPDATE""",
                    input["tenant_id"], input["kb_id"], input["asset_id"],
                )
                if not asset:
                    await conn.execute("ROLLBACK")
                    return None
                if asset["caption_status"] == "disabled":
                    await conn.execute("COMMIT")
                    return None
                existing = await conn.fetchrow(
                    """SELECT * FROM knowledge_caption_jobs
                       WHERE asset_id=$1 AND status IN ('queued','running')
                       ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                    input["asset_id"],
                )
                if existing:
                    await conn.execute("COMMIT")
                    return {"id": str(existing["id"]), "job": dict(existing)}
                job_id = uuid.uuid4()
                job = await conn.fetchrow(
                    """INSERT INTO knowledge_caption_jobs
                       (id, tenant_id, kb_id, asset_id, status, max_attempts)
                       VALUES ($1, $2, $3, $4, 'queued', $5)
                       ON CONFLICT (asset_id) WHERE status IN ('queued','running') DO NOTHING
                       RETURNING *""",
                    job_id, input["tenant_id"], input["kb_id"],
                    input["asset_id"], input.get("max_attempts", 3),
                )
                if not job:
                    fallback = await conn.fetchrow(
                        """SELECT * FROM knowledge_caption_jobs
                           WHERE asset_id=$1 AND status IN ('queued','running')
                           ORDER BY created_at DESC LIMIT 1""",
                        input["asset_id"],
                    )
                    await conn.execute("COMMIT")
                    return {"id": str(fallback["id"]), "job": dict(fallback)} if fallback else None
                await conn.execute(
                    """UPDATE knowledge_assets
                       SET caption_status='queued', caption_updated_at=now(), updated_at=now()
                       WHERE id=$1 AND caption_status IN ('pending','failed')""",
                    input["asset_id"],
                )
                await conn.execute("COMMIT")
                return {"id": str(job["id"]), "job": dict(job)}
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def list_queued_or_stale_caption_jobs(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Invalid limit")
        rows = await self._pool.fetch(
            """SELECT * FROM knowledge_caption_jobs
               WHERE status='queued' OR (status='running' AND lease_expires_at < $1)
               ORDER BY next_attempt_at ASC, created_at ASC
               LIMIT $2""",
            now, limit,
        )
        return [dict(r) for r in rows]

    async def list_pending_orphan_caption_assets(
        self, limit: int, min_size_bytes: int = 1024
    ) -> list[dict[str, str]]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Invalid limit")
        rows = await self._pool.fetch(
            """SELECT a.id, a.kb_id, a.tenant_id
               FROM knowledge_assets a
               WHERE a.deleted_at IS NULL
                 AND a.caption_status = 'pending'
                 AND a.size_bytes >= $2
                 AND NOT EXISTS (
                   SELECT 1 FROM knowledge_caption_jobs j WHERE j.asset_id = a.id
                 )
               ORDER BY a.created_at ASC
               LIMIT $1""",
            limit, min_size_bytes,
        )
        return [{"id": str(r["id"]), "kbId": str(r["kb_id"]), "tenantId": str(r["tenant_id"])} for r in rows]

    async def claim_caption_job(
        self, tenant_id: str, job_id: str, lease_ms: int
    ) -> dict[str, str] | None:
        lease_token = str(uuid.uuid4())
        row = await self._pool.fetchrow(
            """UPDATE knowledge_caption_jobs
               SET status='running', attempts=attempts+1, lease_token=$4::uuid,
                   lease_expires_at=now() + ($3::bigint * interval '1 millisecond'),
                   started_at=COALESCE(started_at, now()), updated_at=now()
               WHERE tenant_id=$1 AND id=$2
                 AND (status='queued' OR (status='running' AND lease_expires_at < now()))
               RETURNING asset_id""",
            tenant_id, job_id, lease_ms, lease_token,
        )
        if not row:
            return None
        self._caption_leases[f"{tenant_id}:{job_id}"] = lease_token
        return {"job_id": job_id, "lease_token": lease_token}

    async def complete_caption_job(
        self,
        tenant_id: str,
        job_id: str,
        token: str,
        result: dict[str, Any],
    ) -> bool:
        if not self._caption_leases.get(f"{tenant_id}:{job_id}"):
            return False
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                job = await conn.fetchrow(
                    """UPDATE knowledge_caption_jobs
                       SET status=$4, model=$5, error_code=$6, error_message=$7,
                           finished_at=now(), lease_token=NULL, lease_expires_at=NULL, updated_at=now()
                       WHERE id=$1 AND tenant_id=$2 AND lease_token=$3::uuid
                         AND lease_expires_at > now() AND status='running'
                       RETURNING asset_id, kb_id""",
                    job_id, tenant_id, token,
                    result["status"], result.get("model"),
                    result.get("error", {}).get("code") if result.get("error") else None,
                    result.get("error", {}).get("message") if result.get("error") else None,
                )
                if not job:
                    await conn.execute("ROLLBACK")
                    return False
                caption_status = (
                    "ready"
                    if result["status"] == "completed"
                    else ("failed" if result.get("error") else "skipped")
                )
                await conn.execute(
                    """UPDATE knowledge_assets
                       SET caption=$3, caption_status=$4, caption_model=$5,
                           caption_error=$6, caption_updated_at=now(), updated_at=now()
                       WHERE tenant_id=$1 AND id=$2 AND kb_id=$7""",
                    tenant_id, job["asset_id"], result.get("caption"),
                    caption_status, result.get("model"),
                    result.get("error", {}).get("message") if result.get("error") else None,
                    job["kb_id"],
                )
                await conn.execute("COMMIT")
                return True
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def fail_caption_job_with_retry(
        self,
        tenant_id: str,
        job_id: str,
        token: str,
        error: dict[str, str],
        backoff_ms: int,
        next_attempt: dict[str, int],
    ) -> dict[str, bool]:
        if not self._caption_leases.get(f"{tenant_id}:{job_id}"):
            return {"requeued": False}
        reached_max = next_attempt["attempts"] >= next_attempt["max_attempts"]
        if reached_max:
            await self.complete_caption_job(tenant_id, job_id, token, {
                "caption": None,
                "model": "",
                "status": "skipped",
                "error": error,
            })
            return {"requeued": False}
        row = await self._pool.fetchrow(
            """UPDATE knowledge_caption_jobs
               SET status='queued', lease_token=NULL, lease_expires_at=NULL,
                   next_attempt_at=now() + ($5::bigint * interval '1 millisecond'),
                   error_code=$3, error_message=$4, updated_at=now()
               WHERE id=$1 AND tenant_id=$2 AND lease_token=$6::uuid
                 AND lease_expires_at > now() AND status='running'
               RETURNING asset_id, kb_id""",
            job_id, tenant_id, error["code"], error["message"],
            backoff_ms, token,
        )
        if not row:
            return {"requeued": False}
        await self._pool.execute(
            """UPDATE knowledge_assets
               SET caption_status='failed', caption_error=$3, caption_updated_at=now(), updated_at=now()
               WHERE tenant_id=$1 AND id=$2""",
            tenant_id, row["asset_id"], error["message"],
        )
        return {"requeued": True}

    async def get_caption_job(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM knowledge_caption_jobs WHERE tenant_id=$1 AND id=$2",
            tenant_id, job_id,
        )
        return dict(row) if row else None

    async def list_document_assets_for_indexing(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        opts = options or {}
        filters = [
            "a.tenant_id=$1",
            "a.kb_id=$2",
            "a.document_id=$3",
            "a.deleted_at IS NULL",
        ]
        values: list[Any] = [tenant_id, kb_id, document_id]
        if opts.get("only_with_caption"):
            filters.append("a.caption IS NOT NULL")
        if opts.get("caption_status"):
            filters.append(f"a.caption_status = ${len(values) + 1}")
            values.append(opts["caption_status"])
        rows = await self._pool.fetch(
            f"""SELECT a.id, a.rel_path, a.name, a.mime, a.caption, a.caption_status, a.document_id
               FROM knowledge_assets a
               WHERE {' AND '.join(filters)}
               ORDER BY a.rel_path ASC""",
            *values,
        )
        return [dict(r) for r in rows]

    async def get_asset_for_caption(
        self, tenant_id: str, asset_id: str
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            """SELECT id, tenant_id, kb_id, document_id, rel_path, name, mime,
                      caption_status, caption, object_key, size_bytes
               FROM knowledge_assets
               WHERE tenant_id=$1 AND id=$2 AND deleted_at IS NULL""",
            tenant_id, asset_id,
        )
        return dict(row) if row else None

    async def enqueue_reindex_if_attached(
        self, tenant_id: str, kb_id: str, document_id: str, reason: str
    ) -> dict[str, str] | None:
        async with self._pool.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                existing = await conn.fetchrow(
                    """SELECT * FROM knowledge_index_jobs
                       WHERE tenant_id=$1 AND document_id=$2 AND status IN ('queued','running')
                       ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                    tenant_id, document_id,
                )
                if existing:
                    await conn.execute("COMMIT")
                    return {"id": str(existing["id"])}
                job_id = uuid.uuid4()
                job = await conn.fetchrow(
                    """INSERT INTO knowledge_index_jobs
                       (id, kb_id, tenant_id, document_id, kind, status)
                       VALUES ($1, $2, $3, $4, 'reindex', 'queued') RETURNING id""",
                    job_id, kb_id, tenant_id, document_id,
                )
                await conn.execute(
                    """UPDATE knowledge_documents SET status='queued', updated_at=now()
                       WHERE tenant_id=$1 AND id=$2 AND status NOT IN ('queued','running','pending')""",
                    tenant_id, document_id,
                )
                await conn.execute("COMMIT")
                return {"id": str(job["id"])}
            except Exception:
                await conn.execute("ROLLBACK")
                raise
