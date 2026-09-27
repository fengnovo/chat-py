"""生产 GraphRAG 检索 — 对应 TS 版 src/retriever.ts。

向量召回 → 命中 chunk 上的实体作为种子 → PG 关系表 BFS → RRF 合并候选 → 拼装引用。
RRF 融合与实体归一化复用 knowledge-graphrag 包实现。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from knowledge_graphrag.graph import normalize_entity_key
from knowledge_graphrag.retriever import MergeLimits, merge_candidates
from knowledge_graphrag.types import GraphRelation, GraphTraversal, VectorHit

MAX_CITATIONS = 20
MAX_RELATIONS_OUTPUT = 20
MAX_RELATION_CHUNKS = 10

_MARKDOWN_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)>\s]+)>?(?:\s+(?:"[^"]*"|\'[^\']*\'))?\s*\)')


@dataclass
class RetrieveParams:
    tenant_id: str
    knowledge_base_ids: list[str]
    query: str
    top_k: int | None = None
    user_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class RetrieverLimits:
    max_hops: int = 2
    max_fanout: int = 20
    max_relations: int = 100
    max_candidates: int = MAX_CITATIONS
    fanout_per_hop: int = 100
    passage_chars: int = 2_000


@dataclass
class ProductionRetrieverDeps:
    pool: Any
    embedder: Any
    vector_store: Any
    repository: Any | None = None
    logger: Any | None = None
    tracer: Any | None = None
    limits: RetrieverLimits = field(default_factory=RetrieverLimits)


def _safely(action: Any) -> None:
    try:
        action()
    except Exception:
        pass


async def _segment(
    tracer: Any,
    name: str,
    action: Any,
    attributes: Optional[dict[str, int]] = None,
) -> Any:
    """为检索子阶段建立子 span。只记录数量类属性；query 原文、SQL 参数、文档正文、向量绝不进入。"""
    if not tracer:
        return await action()
    span = tracer.start_span(name, attributes={"knowledge.component": name, **(attributes or {})})
    try:
        with trace.use_span(span, end_on_exit=True):
            return await action()
    except Exception as error:
        _safely(
            lambda: (
                span.set_status(StatusCode.ERROR),
                # 只放错误构造名，避免 pg 错误携带语句片段。
                span.set_attribute(
                    "error.type", type(error).__name__[:40] if isinstance(error, Exception) else "unknown"
                ),
            )
        )
        raise


def _resolve_asset_rel_path(directory: str | None, path: str) -> str:
    """把 chunk 里记录的图片引用路径拼成 asset 的 kb 级 rel_path。

    正文 chunk 的 path 是相对文档目录的（`000.jpg`），要拼上 directory；
    图片 caption chunk 的 path 已经是 kb 级 rel_path，不能重复拼接。
    """
    if directory and path != "" and not path.startswith(f"{directory}/"):
        return f"{directory}/{path}"
    return path


def strip_dangling_images(passage: str, images: list[dict[str, Any]] | None) -> str:
    """剔除 passage 里匹配不到任何资产的本地图片引用（文档引用了图片但图片从未上传）。

    不剔的话模型会把 `![alt](./000.jpg)` 原样照抄进回答，前端渲染出死链并反复 404。
    外链（http/https/data:）原样保留。
    """
    if not passage or "![" not in passage:
        return passage
    matched = {
        str(image.get("relPath") or image.get("rel_path") or "").split("/")[-1].lower()
        for image in (images or [])
    }

    def _replace(match: re.Match[str]) -> str:
        src = match.group(2)
        if re.match(r"^(https?:|data:)", src, re.IGNORECASE):
            return match.group(0)
        base = src.split("/")[-1].lower()
        return match.group(0) if base in matched else ""

    return _MARKDOWN_IMAGE_RE.sub(_replace, passage)


def _relation_id(source: str, rel_type: str, target: str) -> str:
    return hashlib.sha256(f"{source}\x00{rel_type}\x00{target}".encode()).hexdigest()[:32]


def _row_get(row: Any, key: str) -> Any:
    """兼容 asyncpg.Record / dict 两种行类型。"""
    return row[key]


def _parse_metadata(value: Any) -> dict[str, Any]:
    """jsonb 列在不同驱动/编解码下可能是 str 或 dict，统一归一。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class Retriever:
    """TS createRetriever 返回对象的 Python 对应物。"""

    def __init__(self, deps: ProductionRetrieverDeps) -> None:
        self._deps = deps
        self._limits = deps.limits
        self._collection_ready: Any | None = None

    async def _traverse_graph(
        self,
        tenant_id: str,
        kb_ids: list[str],
        seed_chunk_ids: list[str],
        max_hops: int,
    ) -> dict[str, Any]:
        deps = self._deps
        limits = self._limits
        if not seed_chunk_ids or max_hops <= 0:
            return {"relations": [], "hops": 0}
        seed_rows = await deps.pool.fetch(
            """SELECT DISTINCT entity_key FROM graph_entities
               WHERE tenant_id = $1 AND kb_id = ANY($2::uuid[]) AND chunk_ids && $3::uuid[]""",
            tenant_id, kb_ids, seed_chunk_ids,
        )
        seen_keys = {normalize_entity_key(r["entity_key"]) for r in seed_rows}
        if not seen_keys:
            return {"relations": [], "hops": 0}

        relations_by_id: dict[str, GraphRelation] = {}
        frontier = list(seen_keys)
        hops = 0
        while frontier and hops < max_hops and len(relations_by_id) < limits.max_relations:
            current_hop = hops + 1  # 种子实体本身不算 hop，邻接边记为 hop=1
            rows = await deps.pool.fetch(
                """SELECT source_key, target_key, relation, chunk_ids FROM graph_relationships
                   WHERE tenant_id = $1 AND kb_id = ANY($2::uuid[])
                     AND (source_key = ANY($3::text[]) OR target_key = ANY($3::text[]))
                   ORDER BY source_key, relation, target_key
                   LIMIT $4""",
                tenant_id, kb_ids, frontier, limits.fanout_per_hop,
            )
            next_frontier: list[str] = []
            for row in rows:
                source = normalize_entity_key(row["source_key"])
                target = normalize_entity_key(row["target_key"])
                rel_type = row["relation"].strip()
                rid = _relation_id(source, rel_type, target)
                if rid not in relations_by_id:
                    relations_by_id[rid] = GraphRelation(
                        id=rid,
                        source=source,
                        target=target,
                        type=rel_type,
                        source_chunk_ids=[str(c) for c in (row["chunk_ids"] or [])],
                        hop=current_hop,
                    )
                    if len(relations_by_id) >= limits.max_relations:
                        break
                for key in (source, target):
                    if key not in seen_keys:
                        seen_keys.add(key)
                        next_frontier.append(key)
            hops += 1
            frontier = list(dict.fromkeys(next_frontier))
            if not rows:
                break
        return {"relations": list(relations_by_id.values()), "hops": hops}

    async def retrieve(self, params: RetrieveParams) -> dict[str, Any]:
        deps = self._deps
        limits = self._limits
        started_at = time.monotonic()
        retrieval_id = str(uuid.uuid4())
        kb_ids = list(dict.fromkeys(params.knowledge_base_ids))

        kb_rows = await deps.pool.fetch(
            """SELECT id, top_k, max_hops, graph_enabled FROM knowledge_bases
               WHERE tenant_id = $1 AND deleted_at IS NULL AND id = ANY($2::uuid[])""",
            params.tenant_id, kb_ids,
        )
        kbs = [dict(r) for r in kb_rows]
        empty_stats = {
            "vectorHits": 0,
            "graphHops": 0,
            "searchedKbs": 0,
            "durationMs": int((time.monotonic() - started_at) * 1000),
            "truncated": False,
        }
        if not kbs:
            return {"retrievalId": retrieval_id, "citations": [], "relations": [], "stats": empty_stats}

        kb_default_top_k = min(50, max(int(kb["top_k"]) for kb in kbs))
        top_k = (
            min(50, max(1, round(params.top_k))) if params.top_k else kb_default_top_k
        )
        max_hops = min(limits.max_hops, max(int(kb["max_hops"]) for kb in kbs))
        kb_id_strs = [str(kb["id"]) for kb in kbs]

        if self._collection_ready is None:
            self._collection_ready = deps.vector_store.ensure_collection(deps.embedder.profile)
        await self._collection_ready

        query_vector = await _segment(
            deps.tracer, "knowledge.embed",
            lambda: deps.embedder.embed_query(params.query),
        )
        vector_hits: list[VectorHit] = await _segment(
            deps.tracer, "knowledge.vector.search",
            lambda: deps.vector_store.search(query_vector, params.tenant_id, kb_id_strs, top_k),
            {"kb_count": len(kbs), "top_k": top_k},
        )

        if any(kb.get("graph_enabled") for kb in kbs):
            graph = await _segment(
                deps.tracer, "knowledge.graph.traverse",
                lambda: self._traverse_graph(
                    params.tenant_id, kb_id_strs, [hit.chunk_id for hit in vector_hits], max_hops
                ),
                {"seed_count": len(vector_hits), "max_hops": max_hops},
            )
        else:
            graph = {"relations": [], "hops": 0}

        candidates = merge_candidates(
            vector_hits,
            GraphTraversal(entity_keys=[], relations=graph["relations"], chunk_ids=[]),
            MergeLimits(max_candidates=limits.max_candidates),
        )

        chunk_rows = await _segment(
            deps.tracer, "knowledge.chunks.fetch",
            lambda: deps.pool.fetch(
                """SELECT c.id, c.kb_id, c.document_id, c.ordinal, c.heading, c.text, c.metadata,
                          d.name AS document_name, d.directory AS document_directory
                   FROM knowledge_chunks c
                   JOIN knowledge_documents d ON d.id = c.document_id
                   WHERE c.tenant_id = $1 AND c.id = ANY($2::uuid[]) AND d.deleted_at IS NULL""",
                params.tenant_id, [c.chunk_id for c in candidates],
            ),
            {"candidate_count": len(candidates)},
        )
        chunks_by_id = {str(r["id"]): r for r in chunk_rows}
        valid_candidates = [c for c in candidates if c.chunk_id in chunks_by_id]

        # 收集每个 chunk 命中的图片引用，拼接为 asset 的 kb 级 rel_path，一次批量查出对应 asset。
        ref_lookup: dict[str, dict[str, str]] = {}
        for row in chunk_rows:
            metadata = _parse_metadata(row["metadata"])
            image_refs = metadata.get("imageRefs")
            if not isinstance(image_refs, list):
                continue
            for ref in image_refs:
                if not isinstance(ref, dict) or not ref.get("path"):
                    continue
                rel_path = _resolve_asset_rel_path(row["document_directory"], ref["path"])
                key = f"{row['document_id']}::{rel_path}"
                if key not in ref_lookup:
                    ref_lookup[key] = {
                        "document_id": str(row["document_id"]),
                        "rel_path": rel_path,
                        "alt": ref.get("alt") or "",
                    }

        asset_rows: dict[str, Any] = {}
        if deps.repository and callable(getattr(deps.repository, "list_assets_by_refs", None)) and ref_lookup:
            # 仓库层按属性访问 auth（tenant_id / user_id / roles）
            from types import SimpleNamespace

            auth = SimpleNamespace(
                tenant_id=params.tenant_id,
                user_id=params.user_id or "00000000-0000-0000-0000-000000000000",
                roles=[],
            )
            asset_rows = await deps.repository.list_assets_by_refs(
                auth, kb_id_strs[0], list(ref_lookup.values())
            )

        citation_images: dict[str, list[dict[str, str]]] = {}
        for key, ref in ref_lookup.items():
            asset = asset_rows.get(key)
            if not asset:
                continue
            bucket = citation_images.setdefault(ref["document_id"], [])
            bucket.append({
                "assetId": str(asset["id"]),
                "name": str(asset["name"]),
                "mime": str(asset["mime"]),
                "alt": ref["alt"],
                "relPath": ref["rel_path"],
            })

        citations: list[dict[str, Any]] = []
        for candidate in valid_candidates:
            row = chunks_by_id[candidate.chunk_id]
            metadata = _parse_metadata(row["metadata"])
            image_refs = metadata.get("imageRefs") if isinstance(metadata.get("imageRefs"), list) else []
            images = [
                image
                for image in citation_images.get(str(row["document_id"]), [])
                # 只保留确实由这个 chunk 引用的图（再次过滤避免重提）。
                if any(
                    isinstance(ref, dict)
                    and ref.get("path")
                    and image["relPath"] == _resolve_asset_rel_path(row["document_directory"], ref["path"])
                    for ref in image_refs
                )
            ]
            citation: dict[str, Any] = {
                "chunkId": str(row["id"]),
                "kbId": str(row["kb_id"]),
                "documentId": str(row["document_id"]),
                "documentName": row["document_name"],
                "ordinal": row["ordinal"],
                "score": max(-1.0, min(1.0, candidate.score)),
                "via": "both" if candidate.via == "vector+graph" else candidate.via,
                "passage": strip_dangling_images(str(row["text"])[: limits.passage_chars], images),
                "images": images,
            }
            if row["heading"]:
                citation["heading"] = str(row["heading"])[:500]
            citations.append(citation)

        citation_ids = {c["chunkId"] for c in citations}
        relations = [
            {
                "source": relation.source,
                "relation": relation.type,
                "target": relation.target,
                "chunkIds": [cid for cid in relation.source_chunk_ids if cid in chunks_by_id][
                    :MAX_RELATION_CHUNKS
                ],
            }
            for relation in graph["relations"]
            if any(cid in citation_ids for cid in relation.source_chunk_ids)
        ][:MAX_RELATIONS_OUTPUT]
        relations = [r for r in relations if r["chunkIds"]]

        duration_ms = int((time.monotonic() - started_at) * 1000)
        result = {
            "retrievalId": retrieval_id,
            "citations": citations,
            "relations": relations,
            "stats": {
                "vectorHits": len(vector_hits),
                "graphHops": graph["hops"],
                "searchedKbs": len(kbs),
                "durationMs": duration_ms,
                "truncated": len(citations) >= limits.max_candidates
                or len(relations) >= MAX_RELATIONS_OUTPUT,
            },
        }

        # 检索日志是可观测性旁路：run/session 外的临时 token（如冒烟测试）可能不满足外键，失败不影响检索。
        if (
            deps.repository
            and callable(getattr(deps.repository, "append_retrieval_log", None))
            and params.user_id
            and params.session_id
            and params.run_id
        ):
            try:
                await deps.repository.append_retrieval_log({
                    "retrieval_id": retrieval_id,
                    "tenant_id": params.tenant_id,
                    "user_id": params.user_id,
                    "session_id": params.session_id,
                    "run_id": params.run_id,
                    "kb_ids": kb_ids,
                    "query": params.query,
                    "top_k": top_k,
                    "max_hops": max_hops,
                    "result_count": len(citations),
                    "rerank_status": "disabled",
                    "citations": citations,
                    "latency_ms": duration_ms,
                    "status": "succeeded",
                })
            except Exception as error:
                log_error = getattr(getattr(deps, "logger", None), "error", None)
                if callable(log_error):
                    log_error("append retrieval log failed", error=str(error))
        return result


def create_retriever(deps: ProductionRetrieverDeps) -> Retriever:
    return Retriever(deps)
