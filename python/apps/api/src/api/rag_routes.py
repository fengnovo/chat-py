"""RAG routes — mirrors apps/api/src/rag-routes.ts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import ApiConfig
from .rag_qa import RagQaInput, create_rag_qa_service

router = APIRouter()


class RagStreamBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=-1, le=1)
    history: list[dict[str, str]] | None = Field(default=None, max_length=20)


@router.get("/api/rag/health")
async def rag_health(request: Request):
    config: ApiConfig = request.app.state.config
    rag_service = create_rag_qa_service(config)
    return {"available": rag_service.is_available()}


@router.post("/api/knowledge-bases/{kb_id}/rag")
async def rag_answer(request: Request, kb_id: str, body: RagStreamBody):
    config: ApiConfig = request.app.state.config
    rag_service = create_rag_qa_service(config)
    if not rag_service.is_available():
        raise HTTPException(status_code=503, detail={"error": "rag_qa_unavailable"})
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb = await repo.get_knowledge_base(request.state.auth, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})
    result = await rag_service.answer(
        RagQaInput(
            question=body.question,
            kbId=kb_id,
            tenantId=request.state.auth.tenant_id,
            userId=request.state.auth.user_id,
            topK=body.top_k,
            minScore=body.min_score,
            history=body.history,
        )
    )
    return {
        "answer": result["answer"],
        "citations": result["citations"],
        "model": config.knowledge_qa_model.model if config.knowledge_qa_model else None,
    }


@router.post("/api/knowledge-bases/{kb_id}/rag-stream")
async def rag_stream(request: Request, kb_id: str, body: RagStreamBody):
    config: ApiConfig = request.app.state.config
    rag_service = create_rag_qa_service(config)
    if not rag_service.is_available():
        raise HTTPException(status_code=503, detail={"error": "rag_qa_unavailable"})
    repo = request.app.state.knowledge_repository
    if repo is None:
        raise HTTPException(status_code=503, detail={"error": "knowledge_service_unavailable"})
    kb = await repo.get_knowledge_base(request.state.auth, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail={"error": "knowledge_base_not_found"})

    async def event_stream():
        async for chunk in rag_service.stream(
            RagQaInput(
                question=body.question,
                kbId=kb_id,
                tenantId=request.state.auth.tenant_id,
                userId=request.state.auth.user_id,
                topK=body.top_k,
                minScore=body.min_score,
                history=body.history,
            )
        ):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
