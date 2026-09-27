from typing import Any, Dict, List

from .types import EmbeddingProfile, VectorHit
from .embedder import collection_for_profile


class QdrantChunkStore:
    """Qdrant 向量存储"""

    def __init__(self, client: Any, prefix: str = "knowledge"):
        self.client = client
        self.prefix = prefix
        self.collection_name: str = ""

    async def ensure_collection(self, profile: EmbeddingProfile) -> str:
        """确保 Qdrant collection 存在"""
        derived_name = collection_for_profile(profile, self.prefix)
        if profile.collection_name and profile.collection_name != derived_name:
            raise ValueError(
                f"Embedding profile collection mismatch: expected {derived_name}, received {profile.collection_name}"
            )
        self.collection_name = profile.collection_name or derived_name

        # 获取已有 collections
        existing = await self.client.get_collections()
        names = [c.name for c in existing.collections] if hasattr(existing, "collections") else [c["name"] for c in existing]
        if self.collection_name not in names:
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={"size": profile.dimension, "distance": "Cosine"},
            )
            # 创建 payload 索引
            for field in ("tenant_id", "kb_id", "document_id"):
                await self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema={"type": "keyword", "is_tenant": field == "tenant_id"},
                    wait=True,
                )
        return self.collection_name

    async def upsert(self, points: List[Dict[str, Any]]) -> None:
        """写入向量点"""
        await self.client.upsert(
            collection_name=self.collection_name,
            wait=True,
            points=points,
        )

    async def search(
        self,
        vector: List[float],
        tenant_id: str,
        kb_ids: List[str],
        limit: int,
    ) -> List[VectorHit]:
        """向量检索"""
        filter_ = {
            "must": [
                {"key": "tenant_id", "match": {"value": tenant_id}},
                {"key": "kb_id", "match": {"any": kb_ids}},
            ]
        }
        # 兼容 qdrant-client 新旧 API
        if hasattr(self.client, "query_points"):
            resp = await self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                limit=limit,
                query_filter=filter_,
                with_payload=True,
            )
            rows = resp.points if hasattr(resp, "points") else resp
        elif hasattr(self.client, "query"):
            resp = await self.client.query(
                collection_name=self.collection_name,
                query=vector,
                limit=limit,
                query_filter=filter_,
                with_payload=True,
            )
            rows = resp.points if hasattr(resp, "points") else resp
        else:
            rows = await self.client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                limit=limit,
                query_filter=filter_,
                with_payload=True,
            )

        results: List[VectorHit] = []
        for r in rows:
            payload = getattr(r, "payload", None) or {}
            results.append(
                VectorHit(
                    chunk_id=str(payload.get("chunk_id", getattr(r, "id", ""))),
                    score=getattr(r, "score", 0.0),
                    source_chunk_ids=payload.get("source_chunk_ids"),
                )
            )
        return results

    async def delete_by_document(self, tenant_id: str, kb_id: str, document_id: str) -> None:
        """按文档删除向量"""
        await self.client.delete(
            collection_name=self.collection_name,
            wait=True,
            filter={
                "must": [
                    {"key": "tenant_id", "match": {"value": tenant_id}},
                    {"key": "kb_id", "match": {"value": kb_id}},
                    {"key": "document_id", "match": {"value": document_id}},
                ]
            },
        )


class PostgresGraphStore:
    """Postgres 图谱存储"""

    def __init__(self, repository: Any):
        self.repository = repository

    async def replace_document_graph(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        graph: Any,
    ) -> None:
        """替换文档的图谱数据"""
        await self.repository.replace_document_graph(
            tenant_id, kb_id, document_id, graph
        )
