"""长期记忆语义索引 — 镜像 apps/worker/src/memory-index.ts。

轻量 REST 适配器：复用现有 Qdrant/Embedding 服务，不把索引故障带入主链路。
配置不完整时返回 None，自动降级为仅 PG 记忆。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx


class MemoryIndexConfig:
    """记忆索引配置（镜像 TS MemoryIndexConfig）。"""

    def __init__(
        self,
        *,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
        embedding_url: str | None = None,
        embedding_api_key: str | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
    ) -> None:
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        self.embedding_url = embedding_url
        self.embedding_api_key = embedding_api_key
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension


def _is_uuid(value: str) -> bool:
    return bool(
        re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I)
    )


def _point_id(memory_id: str) -> str:
    if _is_uuid(memory_id):
        return memory_id
    return hashlib.sha256(memory_id.encode()).hexdigest()[:32]


class MemoryIndexer:
    """Qdrant 记忆索引客户端（通过 REST 与 embedding 服务交互）。"""

    def __init__(self, config: MemoryIndexConfig) -> None:
        self._config = config
        self._qdrant = config.qdrant_url.rstrip("/")
        self._collection = f"agent_memory_{config.embedding_dimension}"
        self._ready: bool = False
        self._headers = {
            "content-type": "application/json",
            **({"api-key": config.qdrant_api_key} if config.qdrant_api_key else {}),
        }

    async def _ensure(self, client: httpx.AsyncClient) -> None:
        if self._ready:
            return
        response = await client.get(
            f"{self._qdrant}/collections/{self._collection}", headers=self._headers
        )
        if response.status_code == 200:
            self._ready = True
            return
        created = await client.put(
            f"{self._qdrant}/collections/{self._collection}",
            headers=self._headers,
            json={"vectors": {"size": self._config.embedding_dimension, "distance": "Cosine"}},
        )
        if created.status_code not in (200, 201, 409):
            raise RuntimeError(
                f"Qdrant collection creation failed: {created.status_code}"
            )
        self._ready = True

    async def _embed(self, client: httpx.AsyncClient, text: str) -> list[float]:
        response = await client.post(
            self._config.embedding_url,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._config.embedding_api_key}",
            },
            json={
                "model": self._config.embedding_model,
                "input": [text],
                "dimensions": self._config.embedding_dimension,
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"Memory embedding failed: {response.status_code}")
        body = response.json()
        vector = body.get("data", [{}])[0].get("embedding")
        if not vector or len(vector) != self._config.embedding_dimension:
            raise RuntimeError("Memory embedding dimension mismatch")
        return vector

    async def upsert(
        self,
        *,
        id: str,
        tenant_id: str,
        user_id: str,
        content: str,
        normalized_key: str,
        kind: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        project_id: str | None = None,
        scope: str | None = None,
    ) -> None:
        """写入/更新记忆向量。"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._ensure(client)
            vector = await self._embed(client, content)
            response = await client.put(
                f"{self._qdrant}/collections/{self._collection}/points",
                headers=self._headers,
                json={
                    "points": [
                        {
                            "id": _point_id(id),
                            "vector": vector,
                            "payload": {
                                "memory_id": id,
                                "tenant_id": tenant_id,
                                "user_id": user_id,
                                "project_id": project_id,
                                "scope": scope or "global",
                                "normalized_key": normalized_key,
                                "content": content,
                                "kind": kind,
                                "importance": importance,
                                "confidence": confidence,
                            },
                        }
                    ]
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Memory Qdrant upsert failed: {response.status_code}"
                )

    async def search(
        self,
        query: str,
        tenant_id: str,
        user_id: str,
        limit: int = 8,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """语义检索记忆。"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._ensure(client)
            vector = await self._embed(client, query)
            filters: list[dict[str, Any]] = [
                {"key": "tenant_id", "match": {"value": tenant_id}},
                {"key": "user_id", "match": {"value": user_id}},
            ]
            if project_id:
                filters.append(
                    {"key": "project_id", "match": {"any": [None, project_id]}}
                )
            response = await client.post(
                f"{self._qdrant}/collections/{self._collection}/points/query",
                headers=self._headers,
                json={
                    "query": vector,
                    "limit": limit,
                    "with_payload": True,
                    "filter": {"must": filters},
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Memory Qdrant query failed: {response.status_code}"
                )
            result = response.json()
            points = result.get("result", {}).get("points", [])
            return [
                {
                    "id": str(p.get("payload", {}).get("memory_id", p.get("id"))),
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "project_id": None,
                    "assistant_key": "chat",
                    "scope": str(p.get("payload", {}).get("scope", "global")),
                    "kind": str(p.get("payload", {}).get("kind", "episode")),
                    "content": str(p.get("payload", {}).get("content", "")),
                    "normalized_key": str(
                        p.get("payload", {}).get("normalized_key", p.get("id"))
                    ),
                    "importance": float(p.get("payload", {}).get("importance", 0.5)),
                    "confidence": float(p.get("payload", {}).get("confidence", p.get("score", 0.5))),
                    "status": "active",
                    "source_session_id": None,
                    "source_run_id": None,
                    "supersedes_id": None,
                    "metadata": {"semantic_score": p.get("score", 0)},
                    "created_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                    "updated_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                    "last_accessed_at": None,
                }
                for p in points
            ]

    async def remove(self, memory_id: str) -> None:
        """删除记忆向量。"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._ensure(client)
            response = await client.post(
                f"{self._qdrant}/collections/{self._collection}/points/delete",
                headers=self._headers,
                json={"points": [_point_id(memory_id)], "wait": True},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Memory Qdrant delete failed: {response.status_code}"
                )


def create_memory_indexer(config: MemoryIndexConfig) -> MemoryIndexer | None:
    """创建索引器；配置不完整时返回 None（fail-open）。"""
    if not all(
        [
            config.qdrant_url,
            config.embedding_url,
            config.embedding_api_key,
            config.embedding_model,
            config.embedding_dimension,
        ]
    ):
        return None
    return MemoryIndexer(config)
