"""Database layer for the agent platform — mirrors packages/db.

Provides:
- SQLAlchemy declarative schema models (``schema``)
- Raw-asyncpg ``AgentRepository`` and ``KnowledgeRepository``
- ``migrate_database`` runner
- Password hashing utilities
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .errors import (
    ForbiddenKnowledgeError,
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from .knowledge_repository import (
    KnowledgeEmbeddingProfile,
    KnowledgeRepository,
    KnowledgeRepositoryOptions,
)
from .migrate import migrate_database
from .password import hash_password, verify_password
from .repository import (
    AgentRepository,
    ArtifactRecord,
    CancellationResult,
    ChatAttachmentRecord,
    DispatchOutboxRecord,
    InterruptRecord,
    MemoryJobInput,
    MemoryJobRecord,
    MemoryListInput,
    MemoryRecord,
    ProjectRecord,
    RunRecord,
    SessionListCursor,
    SessionRecord,
    WorkspaceSandboxRecord,
)
from .schema import (
    AgentMemory,
    AgentRun,
    AgentSession,
    Artifact,
    Base,
    ChatAttachment,
    GraphEntity,
    GraphRelationship,
    KnowledgeAsset,
    KnowledgeBase,
    KnowledgeBaseGrant,
    KnowledgeCaptionJob,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeRetrievalLog,
    MemoryJob,
    OAuthAccount,
    Project,
    RunDispatchOutbox,
    RunEvent,
    Tenant,
    TenantMembership,
    User,
    Workspace,
)


@dataclass
class DatabaseHandle:
    """Mirror of packages/db DatabaseHandle — pool + repository pair."""

    pool: asyncpg.Pool
    repository: AgentRepository


async def create_database(connection_string: str) -> DatabaseHandle:
    """Create an asyncpg pool and AgentRepository — mirrors createDatabase()."""
    pool = await asyncpg.create_pool(dsn=connection_string, max_size=20)
    return DatabaseHandle(pool=pool, repository=AgentRepository(pool))


__all__ = [
    "AgentMemory",
    "AgentRepository",
    "AgentRun",
    "AgentSession",
    "Artifact",
    "ArtifactRecord",
    "Base",
    "CancellationResult",
    "ChatAttachment",
    "ChatAttachmentRecord",
    "DispatchOutboxRecord",
    "DatabaseHandle",
    "ForbiddenKnowledgeError",
    "GraphEntity",
    "GraphRelationship",
    "InterruptRecord",
    "KnowledgeAsset",
    "KnowledgeBase",
    "KnowledgeBaseGrant",
    "KnowledgeCaptionJob",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeEmbeddingProfile",
    "KnowledgeIndexJob",
    "KnowledgeRepository",
    "KnowledgeRepositoryOptions",
    "KnowledgeRetrievalLog",
    "MemoryJob",
    "MemoryJobInput",
    "MemoryJobRecord",
    "MemoryListInput",
    "MemoryRecord",
    "OAuthAccount",
    "Project",
    "ProjectRecord",
    "RepositoryConflictError",
    "RepositoryNotFoundError",
    "RunDispatchOutbox",
    "RunEvent",
    "RunRecord",
    "SessionListCursor",
    "SessionRecord",
    "Tenant",
    "TenantMembership",
    "User",
    "Workspace",
    "WorkspaceSandboxRecord",
    "create_database",
    "hash_password",
    "migrate_database",
    "verify_password",
]
