"""Pydantic schemas for the agent platform — mirrors packages/contracts/src/index.ts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── Run Status ──────────────────────────────────────────────────────────────

class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_QUESTION = "waiting_question"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ─── Todo ────────────────────────────────────────────────────────────────────

class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"]


# ─── Approval / Question ─────────────────────────────────────────────────────

class ApprovalAction(BaseModel):
    name: str
    args: dict[str, Any]
    summary: str


class QuestionOption(BaseModel):
    label: str
    description: str | None = None


class UserQuestion(BaseModel):
    question: str
    options: list[QuestionOption] = Field(min_length=2, max_length=9)
    multiple: bool
    allow_custom: bool = Field(alias="allowCustom")

    model_config = ConfigDict(populate_by_name=True)


# ─── Knowledge Base ─────────────────────────────────────────────────────────

def _validate_kb_ids(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(ids)) != len(ids):
        raise ValueError("knowledge base ids must be unique")
    return ids

KnowledgeBaseIds = Annotated[list[uuid.UUID], Field(max_length=10), "unique_items"]

class KnowledgeBaseIdsValidator:
    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def _check(cls, v: Any) -> Any:
        return v


# ─── Citations ───────────────────────────────────────────────────────────────

class CitationImage(BaseModel):
    asset_id: uuid.UUID = Field(alias="assetId")
    name: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=127)
    alt: str = Field(max_length=500)
    rel_path: str = Field(alias="relPath", min_length=1, max_length=1024)

    model_config = ConfigDict(populate_by_name=True)


class Citation(BaseModel):
    chunk_id: uuid.UUID = Field(alias="chunkId")
    kb_id: uuid.UUID = Field(alias="kbId")
    document_id: uuid.UUID = Field(alias="documentId")
    document_name: str = Field(alias="documentName", min_length=1, max_length=255)
    ordinal: int = Field(ge=0)
    heading: str | None = Field(default=None, max_length=500)
    score: float = Field(ge=-1, le=1)
    via: Literal["vector", "graph", "both"]
    images: list[CitationImage] | None = Field(default=None, max_length=20)

    model_config = ConfigDict(populate_by_name=True)


class RelationCitation(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    relation: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    chunk_ids: list[uuid.UUID] = Field(alias="chunkIds", max_length=10)

    model_config = ConfigDict(populate_by_name=True)


class RetrievalStats(BaseModel):
    vector_hits: int = Field(alias="vectorHits", ge=0, le=10_000)
    graph_hops: int = Field(alias="graphHops", ge=0, le=100)
    searched_kbs: int = Field(alias="searchedKbs", ge=0, le=10)
    duration_ms: int = Field(alias="durationMs", ge=0, le=300_000)
    truncated: bool

    model_config = ConfigDict(populate_by_name=True)


# ─── Knowledge Run Token ────────────────────────────────────────────────────

class KnowledgeRunTokenClaims(BaseModel):
    tenant_id: uuid.UUID = Field(alias="tenantId")
    user_id: uuid.UUID = Field(alias="userId")
    session_id: uuid.UUID = Field(alias="sessionId")
    run_id: uuid.UUID = Field(alias="runId")
    kb_ids: list[uuid.UUID] = Field(alias="kbIds", max_length=10)
    jti: uuid.UUID
    exp: int = Field(gt=0)
    aud: Literal["knowledge-service"]

    model_config = ConfigDict(populate_by_name=True)


# ─── Run Capabilities ────────────────────────────────────────────────────────

class RunCapabilities(BaseModel):
    tools: list[str] = Field(max_length=120)
    skills: list[str] = Field(max_length=50)
    backend_mode: Literal["docker", "e2b"] = Field(alias="backendMode")
    context_trigger_tokens: int = Field(alias="contextTriggerTokens", ge=0)
    knowledge_enabled: bool = Field(alias="knowledgeEnabled")

    model_config = ConfigDict(populate_by_name=True)


# ─── Agent Events ────────────────────────────────────────────────────────────

class _EventBase(BaseModel):
    run_id: uuid.UUID = Field(alias="runId")
    timestamp: str  # ISO datetime string

    model_config = ConfigDict(populate_by_name=True)


class RunStartedEvent(_EventBase):
    type: Literal["run.started"] = "run.started"
    capabilities: RunCapabilities | None = None


class AssistantDeltaEvent(_EventBase):
    type: Literal["assistant.delta"] = "assistant.delta"
    text: str


class AssistantReasoningEvent(_EventBase):
    type: Literal["assistant.reasoning"] = "assistant.reasoning"
    text: str


class AssistantNarrationEvent(_EventBase):
    type: Literal["assistant.narration"] = "assistant.narration"
    text: str


class UsageUpdatedEvent(_EventBase):
    type: Literal["usage.updated"] = "usage.updated"
    input_tokens: int = Field(alias="inputTokens", ge=0)
    output_tokens: int = Field(alias="outputTokens", ge=0)
    total_tokens: int = Field(alias="totalTokens", ge=0)

    model_config = ConfigDict(populate_by_name=True)


class ModelRetryEvent(_EventBase):
    type: Literal["model.retry"] = "model.retry"
    model: str
    attempt: int = Field(gt=0)
    delay_ms: int = Field(alias="delayMs", ge=0)
    reason: str

    model_config = ConfigDict(populate_by_name=True)


class ModelFallbackEvent(_EventBase):
    type: Literal["model.fallback"] = "model.fallback"
    from_model: str = Field(alias="from")
    to_model: str = Field(alias="to")
    reason: str

    model_config = ConfigDict(populate_by_name=True)


class ToolStartedEvent(_EventBase):
    type: Literal["tool.started"] = "tool.started"
    invocation_id: str = Field(alias="invocationId")
    tool: str
    input: Any = None

    model_config = ConfigDict(populate_by_name=True)


class ToolCompletedEvent(_EventBase):
    type: Literal["tool.completed"] = "tool.completed"
    invocation_id: str = Field(alias="invocationId")
    tool: str
    output: Any = None

    model_config = ConfigDict(populate_by_name=True)


class RetrievalCompletedEvent(_EventBase):
    type: Literal["retrieval.completed"] = "retrieval.completed"
    retrieval_id: uuid.UUID = Field(alias="retrievalId")
    tool_call_id: str = Field(alias="toolCallId", min_length=1, max_length=255)
    knowledge_base_ids: list[uuid.UUID] = Field(alias="knowledgeBaseIds", max_length=10)
    query: str = Field(min_length=1, max_length=10_000)
    citations: list[Citation] = Field(max_length=20)
    relations: list[RelationCitation] = Field(max_length=20)
    stats: RetrievalStats

    model_config = ConfigDict(populate_by_name=True)


class TodoUpdatedEvent(_EventBase):
    type: Literal["todo.updated"] = "todo.updated"
    todos: list[TodoItem]


class SubagentStartedEvent(_EventBase):
    type: Literal["subagent.started"] = "subagent.started"
    subagent_id: str = Field(alias="subagentId", min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2_000)
    attempt: int = Field(gt=0)
    background: bool | None = None

    model_config = ConfigDict(populate_by_name=True)


class SubagentCompletedEvent(_EventBase):
    type: Literal["subagent.completed"] = "subagent.completed"
    subagent_id: str = Field(alias="subagentId", min_length=1, max_length=64)
    attempt: int = Field(gt=0)
    status: Literal["completed", "failed", "timeout"]
    summary: str = Field(max_length=2_000)
    tool_calls: int = Field(alias="toolCalls", ge=0)
    duration_ms: int = Field(alias="durationMs", ge=0)

    model_config = ConfigDict(populate_by_name=True)


class SubagentReviewedEvent(_EventBase):
    type: Literal["subagent.reviewed"] = "subagent.reviewed"
    subagent_id: str = Field(alias="subagentId", min_length=1, max_length=64)
    attempt: int = Field(gt=0)
    passed: bool
    score: float = Field(ge=0, le=100)
    feedback: str = Field(max_length=1_000)
    checklist: list[dict[str, Any]] = Field(max_length=20)

    model_config = ConfigDict(populate_by_name=True)


class ApprovalRequiredEvent(_EventBase):
    type: Literal["approval.required"] = "approval.required"
    interrupt_id: str = Field(alias="interruptId")
    actions: list[ApprovalAction] = Field(min_length=1)

    model_config = ConfigDict(populate_by_name=True)


class QuestionRequiredEvent(_EventBase):
    type: Literal["question.required"] = "question.required"
    interrupt_id: str = Field(alias="interruptId")
    question: UserQuestion

    model_config = ConfigDict(populate_by_name=True)


class ArtifactCreatedEvent(_EventBase):
    type: Literal["artifact.created"] = "artifact.created"
    artifact_id: uuid.UUID = Field(alias="artifactId")
    name: str
    content_type: str = Field(alias="contentType")

    model_config = ConfigDict(populate_by_name=True)


class RunCompletedEvent(_EventBase):
    type: Literal["run.completed"] = "run.completed"


class RunCancelledEvent(_EventBase):
    type: Literal["run.cancelled"] = "run.cancelled"


class RunFailedEvent(_EventBase):
    type: Literal["run.failed"] = "run.failed"
    code: str
    message: str


class ContextCompressingEvent(_EventBase):
    type: Literal["context.compressing"] = "context.compressing"


AgentEvent = (
    RunStartedEvent
    | AssistantDeltaEvent
    | AssistantReasoningEvent
    | AssistantNarrationEvent
    | UsageUpdatedEvent
    | ModelRetryEvent
    | ModelFallbackEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | RetrievalCompletedEvent
    | TodoUpdatedEvent
    | SubagentStartedEvent
    | SubagentCompletedEvent
    | SubagentReviewedEvent
    | ApprovalRequiredEvent
    | QuestionRequiredEvent
    | ArtifactCreatedEvent
    | RunCompletedEvent
    | RunCancelledEvent
    | RunFailedEvent
    | ContextCompressingEvent
)


class PersistedAgentEvent(BaseModel):
    """AgentEvent + seq (assigned by DB)."""
    seq: int = Field(gt=0)
    event: AgentEvent

    model_config = ConfigDict(populate_by_name=True)


# ─── Session Schemas ─────────────────────────────────────────────────────────

class CreateSessionInput(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=120)
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    external_key: str | None = Field(default=None, alias="externalKey", min_length=1, max_length=200)

    model_config = ConfigDict(populate_by_name=True)


class UpdateSessionInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)


# ─── Project Schemas ─────────────────────────────────────────────────────────

class GitProjectSource(BaseModel):
    type: Literal["git"] = "git"
    url: str
    ref: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def _validate_git_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Only HTTPS Git repositories are supported")
        return v


class EmptyProjectSource(BaseModel):
    type: Literal["empty"] = "empty"


class UploadProjectSource(BaseModel):
    type: Literal["upload"] = "upload"
    object_key: str = Field(alias="objectKey", min_length=1, max_length=1_000)

    model_config = ConfigDict(populate_by_name=True)


WorkspaceSource = EmptyProjectSource | GitProjectSource | UploadProjectSource


class CreateProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source: EmptyProjectSource | GitProjectSource = Field(discriminator="type")


class UploadProjectFile(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    content_base64: str = Field(alias="contentBase64", min_length=1)

    model_config = ConfigDict(populate_by_name=True)


class UploadProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    files: list[UploadProjectFile] = Field(min_length=1, max_length=1_000)


# ─── Run Schemas ─────────────────────────────────────────────────────────────

class CreateRunInput(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    knowledge_base_ids: list[uuid.UUID] = Field(default=[], alias="knowledgeBaseIds", max_length=10)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("knowledge_base_ids")
    @classmethod
    def _unique_kb_ids(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(v)) != len(v):
            raise ValueError("knowledge base ids must be unique")
        return v


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    scope: Literal["once", "session"] = "once"
    message: str | None = Field(default=None, max_length=2_000)


class QuestionSelection(BaseModel):
    index: int = Field(ge=0)
    label: str


class QuestionAnswer(BaseModel):
    selections: list[QuestionSelection]
    custom_text: str | None = Field(default=None, alias="customText", max_length=4_000)

    model_config = ConfigDict(populate_by_name=True)


# ─── Artifact Schemas ───────────────────────────────────────────────────────

class CreateArtifactUploadInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(alias="contentType", min_length=1, max_length=200)
    size_bytes: int = Field(alias="sizeBytes", gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$", max_length=64, min_length=64)

    model_config = ConfigDict(populate_by_name=True)


# ─── Chat Attachment Schemas ────────────────────────────────────────────────

RunAttachmentKind = Literal["image", "text", "file"]


class RunAttachmentRef(BaseModel):
    id: uuid.UUID
    kind: RunAttachmentKind
    object_key: str = Field(alias="objectKey", min_length=1, max_length=600)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(alias="contentType", min_length=1, max_length=200)
    size_bytes: int = Field(alias="sizeBytes", ge=0)
    content_encoding: Literal["gzip"] | None = Field(default=None, alias="contentEncoding")

    model_config = ConfigDict(populate_by_name=True)


class InitChatAttachmentInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(alias="contentType", min_length=1, max_length=200)
    size_bytes: int = Field(alias="sizeBytes", gt=0)
    content_sha256: str = Field(alias="contentSha256", pattern=r"^[a-f0-9]{64}$")
    stored_sha256: str = Field(alias="storedSha256", pattern=r"^[a-f0-9]{64}$")
    stored_size_bytes: int = Field(alias="storedSizeBytes", gt=0)
    content_encoding: Literal["gzip"] | None = Field(default=None, alias="contentEncoding")

    model_config = ConfigDict(populate_by_name=True)


class CompleteChatAttachmentPart(BaseModel):
    number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=255)


class CompleteChatAttachmentInput(BaseModel):
    parts: list[CompleteChatAttachmentPart] | None = None


# ─── Observability Context ──────────────────────────────────────────────────

class ObservabilityContext(BaseModel):
    traceparent: str | None = Field(default=None, max_length=256)
    tracestate: str | None = Field(default=None, max_length=512)
    request_id: str | None = Field(default=None, alias="requestId", max_length=128)

    model_config = ConfigDict(populate_by_name=True)


# ─── Run Job (Queue Payload) ────────────────────────────────────────────────

class RunJobStart(BaseModel):
    kind: Literal["start"] = "start"
    tenant_id: uuid.UUID = Field(alias="tenantId")
    user_id: uuid.UUID = Field(alias="userId")
    session_id: uuid.UUID = Field(alias="sessionId")
    run_id: uuid.UUID = Field(alias="runId")
    message: str
    workspace_path: str = Field(alias="workspacePath")
    workspace_source: WorkspaceSource | None = Field(default=None, alias="workspaceSource")
    approval_mode: Literal["manual", "session"] | None = Field(default=None, alias="approvalMode")
    knowledge_base_ids: list[uuid.UUID] = Field(alias="knowledgeBaseIds", max_length=10)
    attachments: list[RunAttachmentRef] = Field(default=[])
    observability: ObservabilityContext | None = None

    model_config = ConfigDict(populate_by_name=True)


class RunJobResumeApproval(BaseModel):
    kind: Literal["resume-approval"] = "resume-approval"
    tenant_id: uuid.UUID = Field(alias="tenantId")
    user_id: uuid.UUID = Field(alias="userId")
    session_id: uuid.UUID = Field(alias="sessionId")
    run_id: uuid.UUID = Field(alias="runId")
    workspace_path: str = Field(alias="workspacePath")
    decision: ApprovalDecision
    approval_mode: Literal["manual", "session"] | None = Field(default=None, alias="approvalMode")
    knowledge_base_ids: list[uuid.UUID] = Field(alias="knowledgeBaseIds", max_length=10)
    observability: ObservabilityContext | None = None

    model_config = ConfigDict(populate_by_name=True)


class RunJobResumeQuestion(BaseModel):
    kind: Literal["resume-question"] = "resume-question"
    tenant_id: uuid.UUID = Field(alias="tenantId")
    user_id: uuid.UUID = Field(alias="userId")
    session_id: uuid.UUID = Field(alias="sessionId")
    run_id: uuid.UUID = Field(alias="runId")
    workspace_path: str = Field(alias="workspacePath")
    answer: QuestionAnswer
    approval_mode: Literal["manual", "session"] | None = Field(default=None, alias="approvalMode")
    knowledge_base_ids: list[uuid.UUID] = Field(alias="knowledgeBaseIds", max_length=10)
    observability: ObservabilityContext | None = None

    model_config = ConfigDict(populate_by_name=True)


RunJob = RunJobStart | RunJobResumeApproval | RunJobResumeQuestion


# ─── Auth Context ────────────────────────────────────────────────────────────

class AuthContext(BaseModel):
    user_id: uuid.UUID = Field(alias="userId")
    tenant_id: uuid.UUID = Field(alias="tenantId")
    roles: list[str]

    model_config = ConfigDict(populate_by_name=True)


# ─── Queue / Channel Names ──────────────────────────────────────────────────

RUN_QUEUE_NAME = "agent-runs"
MEMORY_QUEUE_NAME = "agent-memory"
MEMORY_INDEX_QUEUE_NAME = "agent-memory-index"


def run_events_channel(run_id: str) -> str:
    return f"agent:run:{run_id}:events"


def run_cancellation_channel(run_id: str) -> str:
    return f"agent:run:{run_id}:cancel"
