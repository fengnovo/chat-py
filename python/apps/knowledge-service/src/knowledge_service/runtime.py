"""运行时组装 — 对应 TS 版 src/runtime.ts。

负责把 config + 依赖组装成 embedder / pipeline；向量存储优先复用
knowledge-graphrag 包的实现，包内尚未提供时回落到本模块内置的
QdrantChunkStore（与 TS 版 store/qdrant.ts 语义一致）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, List, Optional

from knowledge_graphrag.embedder import build_embedding_profile, create_openai_compatible_embedder
from knowledge_graphrag.indexer import IndexPipeline
from knowledge_graphrag.types import EmbeddingProfile, VectorHit

from .config import KnowledgeServiceConfig


# ── Qdrant 向量存储（优先使用 knowledge-graphrag 包内实现，缺失时用本地兜底）────

def _collection_for_profile(profile: EmbeddingProfile, prefix: str = "knowledge") -> str:
    """与 TS collectionForProfile 相同的 collection 命名规则。"""
    digest = hashlib.sha256(f"{profile.key}\0{profile.model}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}_{profile.dimension}"


class QdrantChunkStore:
    """Qdrant chunk 向量存储（对应 TS 版 QdrantChunkStore）。

    接口与 knowledge-graphrag 包内（迁移中的）store 模块保持一致：
    ensure_collection / upsert / search / delete_by_document。
    """

    def __init__(self, client: Any, *, prefix: str = "knowledge") -> None:
        self._client = client
        self._prefix = prefix
        self._collection_name = ""

    @property
    def collection_name(self) -> str:
        return self._collection_name

    async def ensure_collection(self, profile: EmbeddingProfile) -> str:
        derived = _collection_for_profile(profile, self._prefix)
        if profile.collection_name and profile.collection_name != derived:
            raise ValueError(
                f"Embedding profile collection mismatch: expected {derived}, received {profile.collection_name}"
            )
        self._collection_name = profile.collection_name or derived

        from qdrant_client import models as qm

        existing = await self._client.get_collections()
        names = {c.name for c in (existing.collections or [])}
        if self._collection_name not in names:
            await self._client.create_collection(
                self._collection_name,
                vectors_config=qm.VectorParams(size=profile.dimension, distance=qm.Distance.COSINE),
            )
            # tenant_id 建 is_tenant 索引做多租户隔离，kb/document 建 keyword 索引做过滤
            await self._client.create_payload_index(
                self._collection_name, field_name="tenant_id",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            await self._client.create_payload_index(
                self._collection_name, field_name="kb_id",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            await self._client.create_payload_index(
                self._collection_name, field_name="document_id",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        return self._collection_name

    async def upsert(self, points: List[dict]) -> None:
        from qdrant_client import models as qm

        structs = [
            qm.PointStruct(id=p["id"], vector=p["vector"], payload=p.get("payload") or {})
            for p in points
        ]
        await self._client.upsert(self._collection_name, points=structs, wait=True)

    async def search(
        self, vector: List[float], tenant_id: str, kb_ids: List[str], limit: int
    ) -> List[VectorHit]:
        from qdrant_client import models as qm

        query_filter = qm.Filter(
            must=[
                qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=tenant_id)),
                qm.FieldCondition(key="kb_id", match=qm.MatchAny(any=kb_ids)),
            ]
        )
        result = await self._client.query_points(
            self._collection_name, query=vector, limit=limit,
            query_filter=query_filter, with_payload=True,
        )
        hits: List[VectorHit] = []
        for point in result.points:
            payload = point.payload or {}
            hits.append(
                VectorHit(
                    chunk_id=str(payload.get("chunk_id") or point.id),
                    score=float(point.score),
                    source_chunk_ids=payload.get("source_chunk_ids"),
                )
            )
        return hits

    async def delete_by_document(self, tenant_id: str, kb_id: str, document_id: str) -> None:
        from qdrant_client import models as qm

        selector = qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=tenant_id)),
                    qm.FieldCondition(key="kb_id", match=qm.MatchValue(value=kb_id)),
                    qm.FieldCondition(key="document_id", match=qm.MatchValue(value=document_id)),
                ]
            )
        )
        await self._client.delete(self._collection_name, points_selector=selector, wait=True)


def _resolve_chunk_store(client: Any, prefix: str) -> Any:
    """优先使用 knowledge-graphrag 包内的 store 实现（并行迁移中），缺失时用本地实现。"""
    try:
        from knowledge_graphrag.store import QdrantChunkStore as PackageStore  # type: ignore

        return PackageStore(client, prefix=prefix)
    except ImportError:
        return QdrantChunkStore(client, prefix=prefix)


# ── 运行时组装 ────────────────────────────────────────────────────────────

@dataclass
class KnowledgeRuntime:
    """TS KnowledgeRuntime 的 Python 对应物。"""

    profile: EmbeddingProfile
    embedder: Any
    pipeline: Any
    vector_store: Any
    # 透传的协作依赖（repository / download / extract / logger / queue 等）
    extras: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["extras"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def create_knowledge_runtime(
    config: KnowledgeServiceConfig,
    deps: Optional[dict[str, Any]] = None,
) -> KnowledgeRuntime:
    """组装索引运行时；若调用方已提供 pipeline，则原样透传。"""
    deps = dict(deps or {})

    if deps.get("pipeline"):
        return KnowledgeRuntime(
            profile=deps.get("profile"),
            embedder=deps.get("embedder"),
            pipeline=deps["pipeline"],
            vector_store=deps.get("vector_store"),
            extras={k: v for k, v in deps.items() if k not in {"profile", "embedder", "pipeline", "vector_store"}},
        )

    profile = build_embedding_profile(
        key=config.embedding_profile,
        model=config.embedding_model,
        dimension=config.embedding_dimension,
        collection_prefix=config.qdrant_collection_prefix,
    )
    embedder = deps.get("embedder") or create_openai_compatible_embedder(
        profile=profile,
        api_key=config.embedding_api_key,
        base_url=config.embedding_base_url,
    )
    if embedder.profile.collection_name != profile.collection_name:
        raise ValueError(
            f"Embedding profile collection mismatch: expected {profile.collection_name}, "
            f"received {embedder.profile.collection_name}"
        )

    vector_store = deps.get("vector_store")
    pipeline_deps = SimpleNamespace(
        repository=deps.get("repository"),
        download=deps.get("download"),
        extract=deps.get("extract"),
        vector_store=vector_store,
        embedder=embedder,
        logger=deps.get("logger"),
    )
    pipeline = IndexPipeline(pipeline_deps)

    return KnowledgeRuntime(
        profile=profile,
        embedder=embedder,
        pipeline=pipeline,
        vector_store=vector_store,
        extras={k: v for k, v in deps.items() if k not in {"embedder", "vector_store"}},
    )
