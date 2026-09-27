"""Main API routes — mirrors apps/api/src/routes.ts."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from artifacts import (
    ArtifactVerificationError,
    artifact_object_key,
    chat_attachment_object_key,
    project_snapshot_object_key,
)
from contracts import (
    ApprovalDecision,
    AuthContext,
    CompleteChatAttachmentInput,
    CreateArtifactUploadInput,
    CreateProjectInput,
    CreateRunInput,
    CreateSessionInput,
    InitChatAttachmentInput,
    QuestionAnswer,
    RunAttachmentRef,
    UpdateSessionInput,
    UploadProjectInput,
    run_cancellation_channel,
    run_events_channel,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .auth import AuthenticationError, ForbiddenError
from .config import ApiConfig
from .sse import stream_agent_events, stream_workflow_run

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── Helpers ────────────────────────────────────────────────────────────────

MAX_CHAT_ATTACHMENTS = 5
MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TEXT_ATTACHMENT_BYTES = 200_000
MAX_FILE_ATTACHMENT_BYTES = 50 * 1024 * 1024
MULTIPART_THRESHOLD_BYTES = 8 * 1024 * 1024
MULTIPART_PART_BYTES = 5 * 1024 * 1024

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_EXTENSIONS = {
    "txt", "md", "markdown", "json", "csv", "log", "yaml", "yml",
    "xml", "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx",
    "jsx", "py", "sh", "sql", "ini", "conf", "toml",
}

WORKSPACE_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class AttachmentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def resolve_attachment_kind(filename: str, content_type: str, size_bytes: int) -> str:
    if content_type in ALLOWED_IMAGE_TYPES:
        if size_bytes > MAX_IMAGE_ATTACHMENT_BYTES:
            raise AttachmentError("image_attachment_too_large")
        return "image"
    extension = filename.split(".")[-1].lower() if "." in filename else ""
    if content_type.startswith("text/") or extension in TEXT_EXTENSIONS:
        if size_bytes > MAX_TEXT_ATTACHMENT_BYTES:
            raise AttachmentError("text_attachment_too_large")
        return "text"
    if size_bytes > MAX_FILE_ATTACHMENT_BYTES:
        raise AttachmentError("file_attachment_too_large")
    return "file"


def attachment_content_url(attachment_id: str) -> str:
    return f"/api/agent/chat-attachments/{attachment_id}/content"


def attachment_history_view(attachment: Any) -> dict[str, Any]:
    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "contentType": attachment.content_type,
        "sizeBytes": attachment.size_bytes,
        "kind": attachment.kind,
        "url": attachment_content_url(str(attachment.id)),
    }


def encode_session_cursor(cursor: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip("=")


def decode_session_cursor(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        padding = 4 - len(value) % 4
        decoded = base64.urlsafe_b64decode(value + "=" * padding)
        return json.loads(decoded)
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "issues": [{"path": ["cursor"], "message": "Invalid session cursor"}]})


def project_upload_bytes(files: list[dict[str, Any]]) -> int:
    return sum(len(base64.b64decode(f["contentBase64"])) for f in files)


async def remove_session_workspace(
    config: ApiConfig,
    repository: Any,
    workspace: dict[str, Any] | None,
) -> None:
    if not workspace or config.SANDBOX_RUNTIME != "docker":
        return
    workspace_id = workspace.get("workspaceId", "")
    if not workspace_id or not os.path.isdir(Path(config.sandbox_sessions_root) / workspace_id):
        return
    import re
    if not re.match(WORKSPACE_ID_PATTERN, workspace_id, re.IGNORECASE):
        return
    root = Path(config.sandbox_sessions_root).resolve()
    target = (root / workspace_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return
    try:
        import shutil
        shutil.rmtree(target, ignore_errors=True)
    except Exception as e:
        logger.warning("删除会话工作区目录失败 %s: %s", target, e)


# ─── Dependencies ───────────────────────────────────────────────────────────

async def get_auth(request: Request) -> AuthContext:
    auth = getattr(request.state, "auth", None)
    if auth is None:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})
    return auth


# ─── Health ─────────────────────────────────────────────────────────────────

@router.get("/health/live")
async def health_live(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    version = config.API_VERSION
    return {
        "status": "ok",
        "version": version,
        "checks": {"server": {"status": "ok"}},
        "observability": {"enabled": False, "exporter": "disabled"},
    }


@router.get("/health/ready")
async def health_ready(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    repository = request.app.state.repository
    publisher = request.app.state.publisher
    artifacts = request.app.state.artifacts
    version = config.API_VERSION

    checks: dict[str, dict[str, str]] = {}
    failed_component: str | None = None

    deps = [
        ("repository", repository.ping),
        ("publisher", publisher.ping),
        ("artifacts", artifacts.ping),
    ]
    results = await asyncio.gather(*[d[1]() for d in deps], return_exceptions=True)
    for (name, _), result in zip(deps, results):
        if isinstance(result, Exception):
            checks[name] = {"status": "not_ready"}
            if failed_component is None:
                failed_component = name
            logger.error("readiness dependency failed: %s: %s", name, result)
        else:
            checks[name] = {"status": "ok"}

    if failed_component:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "version": version,
                "checks": checks,
                "component": failed_component,
                "error": "dependency_unavailable",
            },
        )
    return {"status": "ready", "version": version, "checks": checks}


# ─── Sessions ───────────────────────────────────────────────────────────────

@router.post("/api/agent/sessions", status_code=201)
async def create_session(
    request: Request,
    input: CreateSessionInput,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    config = request.app.state.config
    workspace_token = str(uuid.uuid4())
    workspace_path = str(Path(config.workspace_root) / str(auth.tenant_id) / workspace_token)
    try:
        session = await repository.create_session(auth, {
            "title": input.title,
            "workspace_path": workspace_path,
            **({"project_id": input.project_id} if input.project_id else {}),
            **({"external_key": input.external_key} if input.external_key else {}),
        })
        return session
    except Exception as e:
        if hasattr(e, "resource"):
            raise HTTPException(status_code=404, detail={"error": f"{e.resource}_not_found"})
        raise


@router.get("/api/agent/sessions")
async def list_sessions(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    decoded_cursor = decode_session_cursor(cursor)
    sessions = await repository.list_sessions(auth, {
        "limit": limit + 1,
        **({"cursor": decoded_cursor} if decoded_cursor else {}),
    })
    has_more = len(sessions) > limit
    data = sessions[:limit] if has_more else sessions
    last = data[-1] if data else None
    return {
        "data": data,
        "nextCursor": encode_session_cursor({"updatedAt": last.updated_at, "id": str(last.id)}) if has_more and last else None,
    }


@router.get("/api/agent/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    session = await repository.get_session(auth, session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    return session


@router.patch("/api/agent/sessions/{session_id}")
async def rename_session(
    session_id: str,
    input: UpdateSessionInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    session = await repository.rename_session(auth, session_id, input.title)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    return session


@router.delete("/api/agent/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> None:
    repository = request.app.state.repository
    config = request.app.state.config
    workspace = await repository.get_workspace_sandbox_for_worker(auth.tenant_id, session_id)
    result = await repository.delete_session(auth, session_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    if result == "active":
        raise HTTPException(status_code=409, detail={"error": "session_has_active_run"})
    await remove_session_workspace(config, repository, workspace)


# ─── Memories ───────────────────────────────────────────────────────────────

@router.get("/api/agent/memories")
async def list_memories(
    request: Request,
    assistant_key: str = "chat",
    scope: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    if scope == "project" and not project_id:
        raise HTTPException(status_code=400, detail={"error": "projectId is required for project scope"})
    repository = request.app.state.repository
    memories = await repository.list_memories({
        "tenant_id": auth.tenant_id,
        "user_id": auth.user_id,
        "assistant_key": assistant_key,
        **({"scope": "global" if scope == "global" else f"project:{project_id}"} if scope else {}),
        **({"project_id": project_id} if project_id else {}),
        "limit": limit,
    })
    return {"data": memories}


@router.delete("/api/agent/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> None:
    repository = request.app.state.repository
    existing = await repository.get_memory(auth.tenant_id, auth.user_id, memory_id)
    await repository.delete_memory(auth.tenant_id, auth.user_id, memory_id)
    queue = request.app.state.memory_index_queue
    if existing and queue:
        await queue.enqueue_job("delete", {"memory_id": memory_id}, _job_id=f"delete:{memory_id}:{int(time.time() * 1000)}")


@router.patch("/api/agent/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    request: Request,
    input: dict[str, Any],
    auth: AuthContext = Depends(get_auth),
) -> Any:
    from memory_core import is_sensitive_memory
    content = input.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    if is_sensitive_memory(content):
        raise HTTPException(status_code=400, detail={"error": "sensitive_memory_rejected"})
    repository = request.app.state.repository
    memory = await repository.update_memory(auth.tenant_id, auth.user_id, memory_id, content)
    if not memory:
        raise HTTPException(status_code=404, detail={"error": "memory_not_found"})
    queue = request.app.state.memory_index_queue
    if queue:
        await queue.enqueue_job("upsert", {"memory": memory.model_dump()}, _job_id=f"upsert:{memory.id}:{memory.updated_at}")
    return memory


@router.delete("/api/agent/memories", status_code=204)
async def clear_memories(
    request: Request,
    assistant_key: str = "chat",
    auth: AuthContext = Depends(get_auth),
) -> None:
    repository = request.app.state.repository
    await repository.clear_memories(auth.tenant_id, auth.user_id, assistant_key)


# ─── Projects ───────────────────────────────────────────────────────────────

@router.get("/api/agent/projects")
async def list_projects(
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    return {"data": await repository.list_projects(auth)}


@router.post("/api/agent/projects", status_code=201)
async def create_project(
    input: CreateProjectInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    project = await repository.create_project(auth, {
        "name": input.name,
        "source_type": input.source.type,
        **({"source_ref": input.source.url} if input.source.type == "git" else {}),
        **({"source_revision": input.source.ref} if input.source.type == "git" and input.source.ref else {}),
    })
    return project


@router.post("/api/agent/projects/upload", status_code=201)
async def upload_project(
    input: UploadProjectInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    config = request.app.state.config
    repository = request.app.state.repository
    artifacts_store = request.app.state.artifacts
    size_bytes = project_upload_bytes([{"contentBase64": f.content_base64} for f in input.files])
    if size_bytes > config.PROJECT_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail={"error": "project_upload_too_large"})
    project_id = str(uuid.uuid4())
    object_key = project_snapshot_object_key(str(auth.tenant_id), project_id)
    manifest = json.dumps({"version": 1, "files": [{"path": f.path, "contentBase64": f.content_base64} for f in input.files]}).encode()
    await artifacts_store.put_object(object_key, manifest, "application/vnd.keen-agent.project+json")
    project = await repository.create_project(auth, {
        "id": project_id,
        "name": input.name,
        "source_type": "upload",
        "source_ref": object_key,
    })
    return project


# ─── Session History & Files ────────────────────────────────────────────────

@router.get("/api/agent/sessions/{session_id}/history")
async def get_session_history(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    session = await repository.get_session(auth, session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    runs = await repository.list_session_runs(auth, session_id)
    event_groups = await asyncio.gather(*[repository.list_events(auth, r.id, 0, 100_000) for r in runs])
    attachments_by_run = await repository.list_chat_attachments_by_runs(auth, [r.id for r in runs])
    messages: list[dict[str, Any]] = []
    for run, events in zip(runs, event_groups):
        assistant_text = "".join(e.payload.get("text", "") for e in events if e.payload.get("type") == "assistant.delta")
        reasoning = "".join(e.payload.get("text", "") for e in events if e.payload.get("type") == "assistant.reasoning")
        citations = [c for e in events if e.payload.get("type") == "retrieval.completed" for c in e.payload.get("citations", [])]
        attachments = [attachment_history_view(a) for a in attachments_by_run.get(run.id, [])]
        if not run.continuation:
            messages.append({
                "id": f"user-{run.id}",
                "runId": str(run.id),
                "role": "user",
                "text": run.user_message,
                "createdAt": run.created_at,
                **({"attachments": attachments} if attachments else {}),
            })
        if assistant_text:
            msg: dict[str, Any] = {
                "id": f"message-{run.id}",
                "runId": str(run.id),
                "role": "assistant",
                "text": assistant_text,
                "createdAt": run.updated_at,
            }
            if reasoning:
                msg["reasoning"] = reasoning
            if citations:
                msg["citations"] = citations
            messages.append(msg)
    return {"session": session, "messages": messages, "latestRun": runs[-1] if runs else None}


@router.get("/api/agent/sessions/{session_id}/files")
async def get_session_files(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    session = await repository.get_session(auth, session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})
    runs = await repository.list_session_runs(auth, session_id)
    event_groups = await asyncio.gather(*[repository.list_events(auth, r.id, 0, 100_000) for r in runs])
    file_ops: dict[str, dict[str, Any]] = {}
    for events in event_groups:
        for event in events:
            payload = event.payload if hasattr(event, "payload") else {}
            if payload.get("type") != "tool.started":
                continue
            tool = payload.get("tool", "")
            if tool not in ("write_file", "edit_file", "read_file", "delete"):
                continue
            args = payload.get("input", {})
            file_path = str(args.get("file_path", args.get("path", ""))).strip()
            if not file_path:
                continue
            content = file_ops.get(file_path, {}).get("content")
            if tool in ("write_file", "edit_file"):
                raw = args.get("content")
                if isinstance(raw, str):
                    content = raw
            file_ops[file_path] = {"path": file_path, "content": content, "operation": tool}
    return {"files": sorted(file_ops.values(), key=lambda f: f["path"])}


# ─── Runs ───────────────────────────────────────────────────────────────────

@router.post("/api/agent/sessions/{session_id}/runs", status_code=202)
async def create_run(
    session_id: str,
    input: CreateRunInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    outbox = request.app.state.outbox
    config = request.app.state.config
    observability = request.app.state.observability

    session = await repository.get_session(auth, session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session_not_found"})

    enqueue = None
    if observability:
        from .observability import start_run_enqueue
        enqueue = start_run_enqueue(observability, request_id=str(uuid.uuid4()), job_kind="start")

    key = request.headers.get("idempotency-key", "").strip()[:200] or None
    try:
        result = await repository.create_run(auth, {
            "session_id": session_id,
            "message": input.message,
            **({"idempotency_key": key} if key else {}),
            "knowledge_base_ids": [str(k) for k in input.knowledge_base_ids],
            **({"observability_context": enqueue.get_context()} if enqueue else {}),
        })
        if enqueue:
            enqueue.finish(run_id=str(result.run.id), outbox_id=result.outbox_id, created=result.created)
        if result.created:
            outbox.wake()
        return result.run
    except Exception as e:
        if enqueue:
            enqueue.finish(created=False, error=e)
        if getattr(e, "pgcode", None) == "23505":
            raise HTTPException(status_code=409, detail={"error": "session_has_active_run"})
        if hasattr(e, "resource"):
            raise HTTPException(status_code=404, detail={"error": f"{e.resource}_not_found"})
        raise


@router.get("/api/agent/runs/{run_id}")
async def get_run(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    repository = request.app.state.repository
    run = await repository.get_run(auth, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    return run


@router.get("/api/agent/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    return await stream_agent_events(
        request, run_id,
        request.app.state.repository, auth,
        request.app.state.stream_subscriptions,
        request.app.state.observability,
    )


@router.post("/api/agent/runs/{run_id}/approvals/{interrupt_id}", status_code=202)
async def approve_run(
    run_id: str,
    interrupt_id: str,
    decision: ApprovalDecision,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    repository = request.app.state.repository
    outbox = request.app.state.outbox
    observability = request.app.state.observability

    run = await repository.get_run(auth, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    if run.status != "waiting_approval":
        raise HTTPException(status_code=409, detail={"error": "run_not_waiting_for_approval"})

    enqueue = None
    if observability:
        from .observability import start_run_enqueue
        enqueue = start_run_enqueue(observability, request_id=str(uuid.uuid4()), job_kind="resume-approval")

    try:
        interrupt = await repository.resolve_interrupt(auth, run_id, interrupt_id, "approval", decision.model_dump())
    except Exception as e:
        if enqueue:
            enqueue.finish(run_id=run_id, created=False, error=e)
        raise
    if not interrupt:
        if enqueue:
            enqueue.finish(run_id=run_id, created=False)
        raise HTTPException(status_code=409, detail={"error": "interrupt_already_resolved"})
    if enqueue:
        enqueue.finish(run_id=run_id, created=True)
    outbox.wake()
    return {"status": "queued"}


@router.post("/api/agent/runs/{run_id}/questions/{interrupt_id}", status_code=202)
async def answer_question(
    run_id: str,
    interrupt_id: str,
    answer: QuestionAnswer,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    repository = request.app.state.repository
    outbox = request.app.state.outbox
    observability = request.app.state.observability

    run = await repository.get_run(auth, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    if run.status != "waiting_question":
        raise HTTPException(status_code=409, detail={"error": "run_not_waiting_for_question"})

    enqueue = None
    if observability:
        from .observability import start_run_enqueue
        enqueue = start_run_enqueue(observability, request_id=str(uuid.uuid4()), job_kind="resume-question")

    try:
        interrupt = await repository.resolve_interrupt(auth, run_id, interrupt_id, "question", answer.model_dump())
    except Exception as e:
        if enqueue:
            enqueue.finish(run_id=run_id, created=False, error=e)
        raise
    if not interrupt:
        if enqueue:
            enqueue.finish(run_id=run_id, created=False)
        raise HTTPException(status_code=409, detail={"error": "interrupt_already_resolved"})
    if enqueue:
        enqueue.finish(run_id=run_id, created=True)
    outbox.wake()
    return {"status": "queued"}


@router.post("/api/agent/runs/{run_id}/cancel", status_code=202)
async def cancel_run(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    repository = request.app.state.repository
    publisher = request.app.state.publisher
    cancellation = await repository.request_cancellation(auth, run_id)
    if not cancellation:
        raise HTTPException(status_code=404, detail={"error": "active_run_not_found"})
    await publisher.publish(run_cancellation_channel(run_id), "cancel")
    if cancellation.get("event"):
        await publisher.publish(run_events_channel(run_id), str(cancellation["event"]["seq"]))
    return {"status": "cancelled" if cancellation["run"].status == "cancelled" else "cancelling"}


# ─── Artifacts ──────────────────────────────────────────────────────────────

@router.post("/api/agent/runs/{run_id}/artifacts", status_code=201)
async def create_artifact(
    run_id: str,
    input: CreateArtifactUploadInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    config = request.app.state.config
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    if input.size_bytes > config.ARTIFACT_MAX_BYTES:
        raise HTTPException(status_code=413, detail={"error": "artifact_too_large"})
    run = await repository.get_run(auth, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    artifact_id = str(uuid.uuid4())
    object_key = artifact_object_key(str(auth.tenant_id), run_id, artifact_id, input.name)
    artifact = await repository.create_artifact(auth, {
        "id": artifact_id,
        "run_id": run_id,
        "name": input.name,
        "object_key": object_key,
        "content_type": input.content_type,
        "size_bytes": input.size_bytes,
        "sha256": input.sha256.lower(),
    })
    upload = await artifacts.create_upload(object_key, input.content_type, input.sha256.lower())
    return {"artifact": artifact, **upload.model_dump()}


@router.post("/api/agent/artifacts/{artifact_id}/complete")
async def complete_artifact(
    artifact_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    publisher = request.app.state.publisher
    artifact = await repository.get_artifact(auth, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail={"error": "artifact_not_found"})
    if artifact.status == "ready":
        return {"artifact": artifact}
    try:
        await artifacts.verify_object(artifact.object_key, {"size_bytes": artifact.size_bytes, "sha256": artifact.sha256})
    except ArtifactVerificationError as e:
        raise HTTPException(status_code=409, detail={"error": "artifact_verification_failed", "message": str(e)})
    ready = await repository.mark_artifact_ready(auth, artifact_id)
    event = await repository.append_event(auth.tenant_id, {
        "run_id": artifact.run_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "type": "artifact.created",
        "artifact_id": artifact_id,
        "name": artifact.name,
        "content_type": artifact.content_type,
    })
    await publisher.publish(run_events_channel(artifact.run_id), str(event.seq))
    return {"artifact": ready}


@router.get("/api/agent/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    artifact = await repository.get_artifact(auth, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail={"error": "artifact_not_found"})
    if artifact.status != "ready":
        raise HTTPException(status_code=409, detail={"error": "artifact_not_ready"})
    expires_in = 300
    download_url = await artifacts.create_download_url(artifact.object_key, expires_in)
    return {
        "artifact": artifact,
        "downloadUrl": download_url,
        "expiresAt": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
    }


# ─── Chat Attachments ───────────────────────────────────────────────────────

@router.post("/api/agent/chat-attachments", status_code=201)
async def create_chat_attachment(
    input: InitChatAttachmentInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    config = request.app.state.config
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    filename = input.filename.strip()[:255] or "attachment"
    content_type = input.content_type.strip().lower()[:200] or "application/octet-stream"
    content_sha256 = input.content_sha256.lower()
    stored_sha256 = input.stored_sha256.lower()

    try:
        kind = resolve_attachment_kind(filename, content_type, input.size_bytes)
    except AttachmentError as e:
        raise HTTPException(status_code=413, detail={"error": e.code})

    # Instant dedup
    existing = await repository.find_ready_chat_attachment_by_content_hash(auth.tenant_id, content_sha256)
    if existing:
        attachment = await repository.create_chat_attachment(auth, {
            "id": str(uuid.uuid4()),
            "object_key": existing.object_key,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": existing.size_bytes,
            "sha256": existing.sha256,
            "content_sha256": content_sha256,
            "content_encoding": existing.content_encoding,
            "kind": kind,
        })
        return {
            "instant": True,
            "mode": "instant",
            "attachment": attachment_history_view(attachment),
        }

    attachment_id = str(uuid.uuid4())
    object_key = chat_attachment_object_key(str(auth.tenant_id), str(auth.user_id), attachment_id, filename)

    if input.stored_size_bytes >= MULTIPART_THRESHOLD_BYTES:
        multipart = await artifacts.create_multipart_upload(object_key, content_type, stored_sha256, {"content_encoding": input.content_encoding})
        attachment = await repository.create_chat_attachment(auth, {
            "id": attachment_id,
            "object_key": object_key,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": input.stored_size_bytes,
            "sha256": stored_sha256,
            "content_sha256": content_sha256,
            "content_encoding": input.content_encoding,
            "upload_id": multipart.upload_id,
            "kind": kind,
        })
        part_count = math.ceil(input.stored_size_bytes / MULTIPART_PART_BYTES)
        parts = await asyncio.gather(*[
            artifacts.presign_part_upload(object_key, multipart.upload_id, i + 1)
            for i in range(part_count)
        ])
        return {
            "instant": False,
            "mode": "multipart",
            "attachment": attachment_history_view(attachment),
            "uploadId": multipart.upload_id,
            "partSize": MULTIPART_PART_BYTES,
            "parts": [p.model_dump() for p in parts],
        }

    attachment = await repository.create_chat_attachment(auth, {
        "id": attachment_id,
        "object_key": object_key,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": input.stored_size_bytes,
        "sha256": stored_sha256,
        "content_sha256": content_sha256,
        "content_encoding": input.content_encoding,
        "kind": kind,
    })
    upload = await artifacts.create_upload(object_key, content_type, stored_sha256, 300)
    return {
        "instant": False,
        "mode": "single",
        "attachment": attachment_history_view(attachment),
        **upload.model_dump(),
    }


@router.post("/api/agent/chat-attachments/{id}/complete")
async def complete_chat_attachment(
    id: str,
    input: CompleteChatAttachmentInput,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    attachment = await repository.get_chat_attachment(auth, id)
    if not attachment:
        raise HTTPException(status_code=404, detail={"error": "attachment_not_found"})
    if attachment.status != "ready":
        if attachment.upload_id:
            if not input.parts:
                raise HTTPException(status_code=400, detail={"error": "parts_required"})
            await artifacts.complete_multipart_upload(attachment.object_key, attachment.upload_id, input.parts)
        try:
            await artifacts.verify_object(attachment.object_key, {"size_bytes": attachment.size_bytes, "sha256": attachment.sha256})
        except ArtifactVerificationError as e:
            raise HTTPException(status_code=409, detail={"error": "attachment_verification_failed", "message": str(e)})
        await repository.mark_chat_attachment_ready(auth, id)
    return {"attachment": attachment_history_view(attachment)}


@router.delete("/api/agent/chat-attachments/{id}", status_code=204)
async def delete_chat_attachment(
    id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> None:
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    attachment = await repository.get_chat_attachment(auth, id)
    if not attachment:
        raise HTTPException(status_code=404, detail={"error": "attachment_not_found"})
    if attachment.run_id:
        raise HTTPException(status_code=409, detail={"error": "attachment_in_use"})
    other_refs = await repository.count_chat_attachments_by_object_key(attachment.object_key, attachment.id)
    if other_refs == 0:
        if attachment.upload_id and attachment.status != "ready":
            await artifacts.abort_multipart_upload(attachment.object_key, attachment.upload_id)
        else:
            await artifacts.delete_object(attachment.object_key)
    await repository.delete_chat_attachment(id)


@router.get("/api/agent/chat-attachments/{id}/content")
async def get_chat_attachment_content(
    id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> RedirectResponse:
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    attachment = await repository.get_chat_attachment(auth, id)
    if not attachment:
        raise HTTPException(status_code=404, detail={"error": "attachment_not_found"})
    if attachment.status != "ready":
        raise HTTPException(status_code=409, detail={"error": "attachment_not_ready"})
    download_url = await artifacts.create_download_url(attachment.object_key, 120)
    return RedirectResponse(url=download_url)


# ─── Chat Endpoint ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    chat_id: str | None = Field(default=None, max_length=200)
    project_id: str | None = None
    knowledge_base_ids: list[str] | None = None
    messages: list[dict[str, Any]]
    attachment_ids: list[str] = Field(default=[], max_length=MAX_CHAT_ATTACHMENTS)
    continuation: bool = False


CONTINUATION_INSTRUCTION = "上一轮任务因执行错误中断了。请基于上方对话和已完成的工作，继续完成上一条用户消息所要求的任务；先检查当前进度，不要重复已经完成的步骤。"


@router.post("/api/chat")
async def chat(
    input: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    config = request.app.state.config
    repository = request.app.state.repository
    outbox = request.app.state.outbox
    knowledge_repo = request.app.state.knowledge_repository
    observability = request.app.state.observability

    if input.continuation and not input.chat_id:
        raise HTTPException(status_code=400, detail={"error": "continuation_requires_session"})

    attachment_refs: list[RunAttachmentRef] = []
    message: str
    if input.continuation:
        message = CONTINUATION_INSTRUCTION
    else:
        last_user = next((m for m in reversed(input.messages) if m.get("role") == "user"), None)
        parts = last_user.get("parts", []) if last_user else []
        if last_user and isinstance(last_user.get("content"), str):
            message = last_user["content"]
        else:
            message = "".join(
                str(p.get("text", "")) for p in parts
                if isinstance(p, dict) and "text" in p
            )
        if input.attachment_ids:
            records = await repository.get_ready_chat_attachments(auth, input.attachment_ids)
            if len(records) != len(set(input.attachment_ids)):
                raise HTTPException(status_code=400, detail={"error": "invalid_attachment"})
            attachment_refs = [
                RunAttachmentRef(
                    id=r.id,
                    kind=r.kind,
                    object_key=r.object_key,
                    filename=r.filename,
                    content_type=r.content_type,
                    size_bytes=r.size_bytes,
                    **({"content_encoding": r.content_encoding} if r.content_encoding else {}),
                )
                for r in records
            ]
        if not message.strip():
            if not attachment_refs:
                raise HTTPException(status_code=400, detail={"error": "message_required"})
            message = "（用户发送了附件，请结合附件内容完成任务）"

    external_key = input.chat_id or str(uuid.uuid4())
    workspace_token = str(uuid.uuid4())
    try:
        if input.continuation:
            existing = await repository.get_session_by_external_key(auth, external_key)
            if not existing:
                raise HTTPException(status_code=404, detail={"error": "session_not_found"})
            session = existing
        else:
            session = await repository.get_or_create_external_session(auth, {
                "external_key": external_key,
                "title": message.strip().split("\n")[0][:120] or "新会话",
                "workspace_path": str(Path(config.workspace_root) / str(auth.tenant_id) / workspace_token),
                **({"project_id": input.project_id} if input.project_id else {}),
            })

        enqueue = None
        if observability:
            from .observability import start_run_enqueue
            enqueue = start_run_enqueue(observability, request_id=str(uuid.uuid4()), job_kind="start")

        try:
            kb_ids = input.knowledge_base_ids
            if kb_ids is None and knowledge_repo:
                bases = await knowledge_repo.list_knowledge_bases(auth)
                kb_ids = [str(b.id) for b in bases]
            elif kb_ids is None:
                kb_ids = []

            result = await repository.create_run(auth, {
                "session_id": str(session.id),
                "message": message.strip(),
                "knowledge_base_ids": kb_ids,
                "attachments": [a.model_dump() for a in attachment_refs],
                "continuation": input.continuation,
                **({"observability_context": enqueue.get_context()} if enqueue else {}),
            })
        except Exception as e:
            if enqueue:
                enqueue.finish(created=False, error=e)
            raise

        if enqueue:
            enqueue.finish(run_id=str(result.run.id), outbox_id=result.outbox_id, created=result.created)
        if result.created:
            outbox.wake()
        return await stream_workflow_run(
            request, str(result.run.id),
            repository, auth,
            request.app.state.stream_subscriptions,
            observability,
        )
    except Exception as e:
        if getattr(e, "pgcode", None) == "23505":
            raise HTTPException(status_code=409, detail={"error": "session_has_active_run"})
        if hasattr(e, "resource"):
            raise HTTPException(status_code=404, detail={"error": f"{e.resource}_not_found"})
        raise


@router.get("/api/chat/{run_id}/stream")
async def chat_stream(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> Any:
    return await stream_workflow_run(
        request, run_id,
        request.app.state.repository, auth,
        request.app.state.stream_subscriptions,
        request.app.state.observability,
    )


# ─── Preview / Rebuild ──────────────────────────────────────────────────────

MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
    ".map": "application/json",
}


@router.get("/api/agent/sessions/{session_id}/preview")
@router.get("/api/agent/sessions/{session_id}/preview/{path:path}")
async def preview(
    session_id: str,
    request: Request,
    path: str = "",
) -> Any:
    config = request.app.state.config
    repository = request.app.state.repository

    # Origin check
    origin = request.headers.get("origin", "") or request.headers.get("referer", "")
    allowed = [config.WEB_ORIGIN, f"http://localhost:{config.API_PORT}", f"http://127.0.0.1:{config.API_PORT}"]
    if not any(origin.startswith(o) for o in allowed):
        logger.info("[preview] blocked_by_origin: %s", origin)
        raise HTTPException(status_code=403, detail={"error": "forbidden"})

    # Find workspace by session external key
    workspace_id = await repository.get_workspace_id_by_external_key(session_id)
    if not workspace_id:
        raise HTTPException(status_code=404, detail={"error": "sandbox_not_found"})
    sandbox_path = Path(config.sandbox_sessions_root) / workspace_id / "user-data" / "workspace"
    if not sandbox_path.is_dir():
        raise HTTPException(status_code=404, detail={"error": "sandbox_not_found"})

    # Find preview root
    candidates = [sandbox_path / "dist", sandbox_path]
    preview_root = None
    for c in candidates:
        if (c / "index.html").is_file():
            preview_root = c
            break
    if not preview_root:
        for entry in sandbox_path.iterdir():
            if entry.is_dir() and (entry / "dist" / "index.html").is_file():
                preview_root = entry / "dist"
                break
    if not preview_root:
        raise HTTPException(status_code=404, detail={"error": "no_preview_built"})

    request_path = path or "index.html"
    safe_path = Path(request_path).resolve()
    if not str(safe_path).startswith("/"):
        safe_path = Path("/") / safe_path
    file_path = (preview_root / safe_path.relative_to("/")).resolve()
    try:
        file_path.relative_to(preview_root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail={"error": "forbidden"})

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    ext = file_path.suffix.lower()
    content_type = MIME_TYPES.get(ext, "application/octet-stream")
    from fastapi.responses import FileResponse
    return FileResponse(file_path, media_type=content_type, headers={"Cache-Control": "no-cache"})


SANDBOX_IMAGE = os.environ.get("DOCKER_SANDBOX_IMAGE", "chat-agent-sandbox:latest")
DOCKER_WORKSPACE = "/mnt/user-data/workspace"


@router.post("/api/agent/sessions/{session_id}/rebuild")
async def rebuild(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    config = request.app.state.config
    repository = request.app.state.repository
    session = await repository.get_session_by_external_key(auth, session_id)
    resolved_session_id = str(session.id) if session else session_id

    workspace = await repository.get_workspace_sandbox_for_worker(auth.tenant_id, resolved_session_id)
    if not workspace or not workspace.get("workspaceId"):
        raise HTTPException(status_code=404, detail={"error": "sandbox_not_found"})
    if config.SANDBOX_RUNTIME != "docker":
        raise HTTPException(status_code=400, detail={"error": "rebuild_requires_docker"})

    sandbox_path = Path(config.sandbox_sessions_root) / workspace["workspaceId"] / "user-data" / "workspace"
    if not sandbox_path.exists():
        raise HTTPException(status_code=404, detail={"error": "sandbox_not_found"})

    container_name = f"rebuild-{session_id[:8]}-{uuid.uuid4().hex[:8]}"
    build_script = (
        "set -e && "
        f"cd {DOCKER_WORKSPACE} && "
        'PROJECT_DIR="." && '
        'if [ ! -f package.json ]; then '
        'for d in */; do if [ -f "${d}package.json" ]; then PROJECT_DIR="$d"; break; fi; done; '
        'fi && '
        f'cd "$PROJECT_DIR" && npx vite build --base ./ --outDir {DOCKER_WORKSPACE}/dist 2>&1'
    )

    args = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "768m",
        "--cpus", "1.5",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
        "--user", "65532:65532",
        "--env", "HOME=/tmp",
        "--workdir", DOCKER_WORKSPACE,
        "--mount", f"type=bind,src={sandbox_path},dst=/mnt/user-data",
        SANDBOX_IMAGE,
        "/bin/bash", "-lc", build_script,
    ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_data = bytearray()
    max_bytes = 100_000
    while len(stdout_data) < max_bytes:
        chunk = await proc.stdout.read(8192) if proc.stdout else b""
        if not chunk:
            break
        stdout_data.extend(chunk[:max_bytes - len(stdout_data)])
    exit_code = await proc.wait()
    output = stdout_data.decode("utf-8", errors="replace")
    if exit_code == 0:
        return {"status": "ok", "previewUrl": f"/api/agent/sessions/{session_id}/preview/"}
    raise HTTPException(status_code=500, detail={"error": "build_failed", "output": output[:5000]})
