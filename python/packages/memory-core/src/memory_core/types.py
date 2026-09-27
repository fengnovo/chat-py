"""Memory domain types — mirrors the memory data model."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MemoryScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"  # stored as "project:{projectId}"

    @staticmethod
    def for_project(project_id: uuid.UUID | str) -> str:
        return f"project:{project_id}"


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    CONTEXT = "context"
    INSTRUCTION = "instruction"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MemoryRecord(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID = Field(alias="tenantId")
    user_id: uuid.UUID = Field(alias="userId")
    project_id: uuid.UUID | None = Field(default=None, alias="projectId")
    assistant_key: str = Field(alias="assistantKey")
    scope: str
    kind: str
    content: str
    normalized_key: str = Field(alias="normalizedKey")
    importance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    status: str
    source_session_id: uuid.UUID | None = Field(default=None, alias="sourceSessionId")
    source_run_id: uuid.UUID | None = Field(default=None, alias="sourceRunId")
    supersedes_id: uuid.UUID | None = Field(default=None, alias="supersedesId")
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_accessed_at: datetime | None = Field(default=None, alias="lastAccessedAt")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)
