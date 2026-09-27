"""Knowledge base routes — mirrors apps/api/src/knowledge-routes.ts."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from artifacts import ArtifactVerificationError, assert_image_magic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .config import ApiConfig
from .knowledge_assistant import answer_with_citations, search_knowledge

router = APIRouter()

# ─── Constants ───────────────────────────────────────────────────────────

MIN_KNOWLEDGE_ASSET_BYTES = 1024
MULTIPART_THRESHOLD_BYTES = 8 * 1024 * 1024
MULTIPART_PART_BYTES = 5 * 1024 * 1024

ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HASH_RE = re.compile(r"^[a-f0-9]{64}$", re.I)

DOCUMENT_MIMES = {
    "text/markdown",
    "text/plain",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ASSET_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _is_uuid(value: str) -> bool:
    return bool(ID_RE.match(value))


def _is_hash(value: str) -> bool:
    return bool(HASH_RE.match(value))


def _parse_uuid(value: str) -> str:
    if not _is_uuid(value):
        raise HTTPException(status_code=400, detail={"error": "invalid_uuid"})
    return value


class DirectoryField:
    """knowledge_bases 内子目录路径，长度上限 + 不允许开头斜杠、连续斜杠。"""

    @classmethod
    def validate(cls, v: str) -> str:
        if not v:
            return v
        if v.startswith("/"):
            raise ValueError("directory must not start with /")
        v = re.sub(r"/+", "/", v).rstrip("/")
        if len(v) > 500:
            raise ValueError("directory too long")
        return v


# ─── Pydantic input schemas ───────────────────────────────────────────────

class CreateKnowledgeBaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: str = Field(default="private")


class UpdateKnowledgeBaseInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None)
    visibility: str | None = Field(default=None)


class RenameDocumentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class DocumentUploadInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mime: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    stored_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    stored_size_bytes: int | None = Field(default=None, gt=0)
    content_encoding: str | None = Field(default=None)
    kind: str = Field(default="document")
    directory: str | None = Field(default=None)
    rel_path: str | None = Field(default=None, max_length=500)
    document_id: str | None = Field(default=None)

    @field_validator("directory")
    @classmethod
    def _validate_directory(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return DirectoryField.validate(v)


class ConfirmUploadInput(BaseModel):
    size_bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    parts: list[dict[str, Any]] | None = Field(default=None)


class ConfirmAssetInput(BaseModel):
    size_bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    metadata: dict[str, Any] | None = Field(default=None)
    parts: list[dict[str, Any]] | None = Field(default=None)


class ChunkQuery(BaseModel):
    q: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class AssetQuery(BaseModel):
    document_id: str | None = Field(default=None)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class RetrievalInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=-1, le=1)


class AskInput(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=-1, le=1)


# ─── Helpers ─────────────────────────────────────────────────────────────

async def _presign_knowledge_upload(
    request: Request,
    object_key: str,
    mime: str,
    stored_sha256: str,
    stored_size_bytes: int,
    content_encoding: str | None,
    entity_key: str,
    entity: Any,
    on_multipart: Any,
) -> Any:
    artifacts = request.app.state.artifacts
    if stored_size_bytes >= MULTIPART_THRESHOLD_BYTES:
        multipart = await artifacts.create_multipart_upload(
            object_key, mime, stored_sha256,
            content_encoding=content_encoding,
        )
        upload_id = multipart["upload_id"]
        await on_multipart(upload_id)
        part_count = (stored_size_bytes + MULTIPART_PART_BYTES - 1) // MULTIPART_PART_BYTES
        parts = await asyncio.gather(*[
            artifacts.presign_part_upload(object_key, upload_id, i + 1)
            for i in range(part_count)
        ])
        return {
            "mode": "multipart",
            entity_key: entity,
            "uploadId": upload_id,
            "partSize": MULTIPART_PART_BYTES,
            "parts": parts,
        }
    upload = await artifacts.create_upload(object_key, mime, stored_sha256)
    return {"mode": "single", entity_key: entity, "upload": upload}


async def _enqueue_index_job(request: Request, result: dict[str, Any]) -> None:
    job = result.get("job")
    if not job or not job.get("id"):
        return
    queue = request.app.state.knowledge_queue
    await queue.enqueue_job("index", job)



# ─── Routes ──────────────────────────────────────────────────────────────

@router.get("/api/knowledge-bases")
async def list_knowledge_bases(request: Request):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    return {"data": await repo.list_knowledge_bases(request.state.auth)}


@router.post("/api/knowledge-bases", status_code=201)
async def create_knowledge_base(request: Request, input: CreateKnowledgeBaseInput):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    result = await repo.create_knowledge_base(request.state.auth, input.model_dump())
    return result


@router.get("/api/knowledge-bases/{kb_id}")
async def get_knowledge_base(request: Request, kb_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    result = await repo.get_knowledge_base(request.state.auth, _parse_uuid(kb_id))
    if not result:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    return result


@router.delete("/api/knowledge-bases/{kb_id}", status_code=204)
async def delete_knowledge_base(request: Request, kb_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    ok = await repo.delete_knowledge_base(request.state.auth, _parse_uuid(kb_id))
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    return None


@router.patch("/api/knowledge-bases/{kb_id}")
async def update_knowledge_base(request: Request, kb_id: str, input: UpdateKnowledgeBaseInput):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    data = {k: v for k, v in input.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail={"error": "empty_patch"})
    result = await repo.update_knowledge_base(request.state.auth, _parse_uuid(kb_id), data)
    if not result:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    return result


@router.get("/api/knowledge-bases/{kb_id}/documents")
async def list_knowledge_documents(request: Request, kb_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    return {"data": await repo.list_knowledge_documents(request.state.auth, _parse_uuid(kb_id))}


@router.get("/api/knowledge-bases/{kb_id}/documents/{document_id}")
async def get_knowledge_document(request: Request, kb_id: str, document_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    result = await repo.get_knowledge_document(
        request.state.auth, _parse_uuid(kb_id), _parse_uuid(document_id)
    )
    if not result:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    return result


@router.delete("/api/knowledge-bases/{kb_id}/documents/{document_id}", status_code=204)
async def delete_knowledge_document(request: Request, kb_id: str, document_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    ok = await repo.delete_knowledge_document(
        request.state.auth, _parse_uuid(kb_id), _parse_uuid(document_id)
    )
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    return None


@router.patch("/api/knowledge-bases/{kb_id}/documents/{document_id}")
async def rename_knowledge_document(request: Request, kb_id: str, document_id: str, input: RenameDocumentInput):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    result = await repo.rename_knowledge_document(
        request.state.auth, _parse_uuid(kb_id), _parse_uuid(document_id), input.name
    )
    if not result:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    return result


@router.get("/api/knowledge-bases/{kb_id}/documents/{document_id}/chunks")
async def list_document_chunks(request: Request, kb_id: str, document_id: str, query: ChunkQuery = Depends()):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    document = await repo.get_knowledge_document(
        request.state.auth, _parse_uuid(kb_id), _parse_uuid(document_id)
    )
    if not document:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    result = await repo.list_document_chunks(
        request.state.auth, _parse_uuid(kb_id), _parse_uuid(document_id),
        {"search": query.q, "limit": query.limit, "offset": query.offset}
    )
    return {"data": result["rows"], "total": result["total"]}


@router.post("/api/knowledge-bases/{kb_id}/documents/uploads", status_code=201)
async def upload_knowledge_document(request: Request, kb_id: str, input: DocumentUploadInput):
    config: ApiConfig = request.app.state.config
    repo = request.app.state.knowledge_repository
    artifacts = request.app.state.artifacts
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb_uuid = _parse_uuid(kb_id)
    if not await repo.can_write_knowledge_base(request.state.auth, kb_uuid):
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    if input.size_bytes > config.KNOWLEDGE_DOCUMENT_MAX_BYTES:
        raise HTTPException(status_code=400, detail={"error": "knowledge_document_too_large"})
    stored_sha256 = input.stored_sha256 or input.sha256
    stored_size_bytes = input.stored_size_bytes or input.size_bytes
    content_encoding = input.content_encoding

    if input.kind == "asset":
        if input.mime not in ASSET_MIMES:
            raise HTTPException(status_code=400, detail={"error": "asset_mime_unsupported"})
        if input.size_bytes < MIN_KNOWLEDGE_ASSET_BYTES:
            raise HTTPException(status_code=400, detail={"error": "asset_too_small", "minBytes": MIN_KNOWLEDGE_ASSET_BYTES})
        rel_path = input.rel_path or input.name
        asset_id = str(uuid.uuid4())
        object_key = f"tenants/{request.state.auth.tenant_id}/knowledge/{kb_id}/assets/{asset_id}/{input.name}"
        created = await repo.create_asset_upload(
            request.state.auth,
            {
                "kbId": kb_uuid,
                "assetId": asset_id,
                "relPath": rel_path,
                "name": input.name,
                "mime": input.mime,
                "sizeBytes": input.size_bytes,
                "sha256": input.sha256,
                "storedSha256": stored_sha256,
                "storedSizeBytes": stored_size_bytes,
                "contentEncoding": content_encoding,
                "objectKey": object_key,
            }
        )
        if not created:
            raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
        if created.get("id") != asset_id and (created.get("uploaded_at") or created.get("uploadedAt")):
            return {"mode": "instant", "asset": created}
        return await _presign_knowledge_upload(
            request,
            created.get("object_key") or created.get("objectKey"),
            input.mime,
            stored_sha256,
            stored_size_bytes,
            content_encoding,
            "asset",
            created,
            lambda upload_id: repo.set_knowledge_asset_upload_id(
                request.state.auth.tenant_id, created["id"], upload_id
            ),
        )

    if input.mime not in DOCUMENT_MIMES:
        raise HTTPException(status_code=400, detail={"error": "document_mime_unsupported"})
    document_id = input.document_id or str(uuid.uuid4())
    created = await repo.create_document_upload(
        request.state.auth,
        {
            **input.model_dump(),
            "kbId": kb_uuid,
            "documentId": document_id,
            "storedSha256": stored_sha256,
            "storedSizeBytes": stored_size_bytes,
            "contentEncoding": content_encoding,
            "objectKey": f"tenants/{request.state.auth.tenant_id}/knowledge/{kb_id}/{document_id}/{input.name}",
        }
    )
    if not created:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    object_key = created.get("object_key") or created.get("objectKey")

    if created.get("id") != document_id and created.get("status") != "pending":
        if created.get("status") != "failed":
            return {"mode": "instant", "document": created}
        # 上次索引失败：对象仍在的话直接重新校验并入队重试，不再传输字节。
        try:
            await artifacts.verify_object(object_key, stored_size_bytes, stored_sha256)
            object_intact = True
        except ArtifactVerificationError:
            object_intact = False
        if object_intact:
            result = await repo.confirm_document_upload(request.state.auth, kb_uuid, created["id"], {"sizeBytes": stored_size_bytes, "sha256": stored_sha256})
            if result and result.get("created") is not False:
                await _enqueue_index_job(request, result)
            return {"mode": "instant", "document": result.get("document") or created}

    return await _presign_knowledge_upload(
        request,
        object_key,
        input.mime,
        stored_sha256,
        stored_size_bytes,
        content_encoding,
        "document",
        created,
        lambda upload_id: repo.set_knowledge_document_upload_id(
            request.state.auth.tenant_id, created["id"], upload_id
        ),
    )


@router.post("/api/knowledge-bases/{kb_id}/documents/{document_id}/confirm")
async def confirm_document_upload(request: Request, kb_id: str, document_id: str, input: ConfirmUploadInput):
    config: ApiConfig = request.app.state.config
    repo = request.app.state.knowledge_repository
    artifacts = request.app.state.artifacts
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb_uuid = _parse_uuid(kb_id)
    doc_uuid = _parse_uuid(document_id)
    if not await repo.can_write_knowledge_base(request.state.auth, kb_uuid):
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    if input.size_bytes > config.KNOWLEDGE_DOCUMENT_MAX_BYTES:
        raise HTTPException(status_code=400, detail={"error": "knowledge_document_too_large"})
    document = await repo.get_knowledge_document(request.state.auth, kb_uuid, doc_uuid)
    if not document:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    object_key = document.get("object_key") or document.get("objectKey")
    upload_id = document.get("upload_id") or document.get("uploadId")
    if upload_id:
        if not input.parts:
            raise HTTPException(status_code=400, detail={"error": "parts_required"})
        await artifacts.complete_multipart_upload(object_key, upload_id, input.parts)
    try:
        await artifacts.verify_object(object_key, input.size_bytes, input.sha256)
    except ArtifactVerificationError:
        raise HTTPException(status_code=400, detail={"error": "document_verification_failed"})
    result = await repo.confirm_document_upload(request.state.auth, kb_uuid, doc_uuid, {"sizeBytes": input.size_bytes, "sha256": input.sha256})
    if not result:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})
    if result.get("created") is not False:
        await _enqueue_index_job(request, result)
    return result.get("document") or result


@router.post("/api/knowledge-bases/{kb_id}/assets/{asset_id}/confirm")
async def confirm_asset_upload(request: Request, kb_id: str, asset_id: str, input: ConfirmAssetInput):
    config: ApiConfig = request.app.state.config
    repo = request.app.state.knowledge_repository
    artifacts = request.app.state.artifacts
    caption_queue = request.app.state.caption_queue
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb_uuid = _parse_uuid(kb_id)
    asset_uuid = _parse_uuid(asset_id)
    if input.size_bytes > config.KNOWLEDGE_DOCUMENT_MAX_BYTES:
        raise HTTPException(status_code=400, detail={"error": "asset_too_large"})
    asset = await repo.get_knowledge_asset(request.state.auth, kb_uuid, asset_uuid)
    if not asset:
        raise HTTPException(status_code=404, detail={"error": "asset_not_found"})
    asset_upload_id = asset.get("upload_id") or asset.get("uploadId")
    if asset_upload_id:
        if not input.parts:
            raise HTTPException(status_code=400, detail={"error": "parts_required"})
        await artifacts.complete_multipart_upload(asset["object_key"], asset_upload_id, input.parts)
    try:
        await artifacts.verify_object(asset["object_key"], input.size_bytes, input.sha256)
    except ArtifactVerificationError:
        raise HTTPException(status_code=400, detail={"error": "asset_verification_failed"})
    # 第二道闸门：按声明的 MIME 校验对象前 12 字节的魔数
    try:
        head = await artifacts.get_object_head(asset["object_key"], 16)
        assert_image_magic(head, asset["mime"])
    except ArtifactVerificationError as e:
        raise HTTPException(status_code=400, detail={"error": "asset_magic_check_failed", "message": str(e)})
    result = await repo.confirm_asset_upload(request.state.auth, kb_uuid, asset_uuid, input.model_dump())
    if not result:
        raise HTTPException(status_code=404, detail={"error": "asset_not_found"})
    if config.caption_enabled and caption_queue and re.match(r"^(image|photo)/", result.get("asset", {}).get("mime", "")):
        try:
            enqueued = await repo.enqueue_caption_job({
                "tenantId": request.state.auth.tenant_id,
                "kbId": kb_uuid,
                "assetId": asset_uuid,
            })
            if enqueued:
                await caption_queue.enqueue_job("caption-asset", {
                    "tenantId": request.state.auth.tenant_id,
                    "jobId": enqueued["id"],
                })
        except Exception:
            pass
    return result.get("asset")


@router.get("/api/knowledge-bases/{kb_id}/assets")
async def list_knowledge_assets(request: Request, kb_id: str, query: AssetQuery = Depends()):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    rows = await repo.list_knowledge_assets(
        request.state.auth, _parse_uuid(kb_id), query.model_dump()
    )
    return {"data": rows}


@router.delete("/api/knowledge-bases/{kb_id}/assets/{asset_id}", status_code=204)
async def delete_knowledge_asset(request: Request, kb_id: str, asset_id: str):
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    ok = await repo.delete_knowledge_asset(request.state.auth, _parse_uuid(kb_id), _parse_uuid(asset_id))
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "asset_not_found"})
    return None


@router.get("/api/knowledge-bases/{kb_id}/assets/{asset_id}/content")
async def get_knowledge_asset_content(request: Request, kb_id: str, asset_id: str):
    """流式返回资源二进制。带鉴权 + 走 api 代理，避开预签名 URL 过期 + CORS 问题。"""
    repo = request.app.state.knowledge_repository
    artifacts = request.app.state.artifacts
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    asset = await repo.get_knowledge_asset(request.state.auth, _parse_uuid(kb_id), _parse_uuid(asset_id))
    if not asset:
        raise HTTPException(status_code=404, detail={"error": "asset_not_found"})
    try:
        bytes_data = await artifacts.get_object_bytes(asset["object_key"])
    except Exception:
        raise HTTPException(status_code=502, detail={"error": "asset_fetch_failed"})
    return Response(content=bytes_data, media_type=asset["mime"], headers={"cache-control": "private, max-age=600"})


async def _run_retrieval(request: Request, kb_id: str, body: RetrievalInput):
    config: ApiConfig = request.app.state.config
    repo = request.app.state.knowledge_repository
    if not config.knowledge_mcp:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb = await repo.get_knowledge_base(request.state.auth, _parse_uuid(kb_id))
    if not kb:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    try:
        result = await search_knowledge(
            config.knowledge_mcp,
            {"tenantId": request.state.auth.tenant_id, "userId": request.state.auth.user_id, "kbIds": [kb_id]},
            query=body.query,
            top_k=body.top_k,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "knowledge_retrieval_failed", "message": str(e)[:200]})
    min_score = body.min_score if body.min_score is not None else 0
    citations = [c for c in result.get("citations", []) if c.get("score", 0) >= min_score]
    return {**result, "citations": citations}


@router.post("/api/knowledge-bases/{kb_id}/retrieval")
async def knowledge_retrieval(request: Request, kb_id: str, body: RetrievalInput):
    return await _run_retrieval(request, kb_id, body)


@router.post("/api/knowledge-bases/{kb_id}/ask")
async def knowledge_ask(request: Request, kb_id: str, body: AskInput):
    config: ApiConfig = request.app.state.config
    if not config.knowledge_qa_model:
        raise HTTPException(status_code=503, detail={"error": "knowledge_qa_unavailable"})
    retrieved = await _run_retrieval(request, kb_id, RetrievalInput(query=body.question, top_k=body.top_k, min_score=body.min_score))
    answer = await answer_with_citations(
        config.knowledge_qa_model,
        question=body.question,
        citations=retrieved.get("citations", []),
        kb_id=kb_id,
    )
    return {"answer": answer, "retrieval": retrieved}
