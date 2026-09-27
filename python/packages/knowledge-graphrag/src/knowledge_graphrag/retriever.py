from typing import List, Optional

from .types import CandidateEvidence, GraphTraversal, VectorHit

# 默认 RRF k 值。k 越大，对低 rank 的平滑越强（即对顶端结果的偏好越弱）。
# 60 是 Cormack 等人 (2009) 的常用经验值，能在召回与精确之间取得较稳定平衡。
DEFAULT_RRF_K = 60


class MergeLimits:
    """融合限制"""

    def __init__(self, max_candidates: int, rrf_k: Optional[int] = None):
        if not isinstance(max_candidates, int) or max_candidates < 0:
            raise ValueError("Invalid candidate limit")
        self.max_candidates = max_candidates
        self.rrf_k = rrf_k if rrf_k is not None else DEFAULT_RRF_K


def merge_candidates(
    vector_hits: List[VectorHit],
    graph_result: GraphTraversal,
    limits: MergeLimits,
) -> List[CandidateEvidence]:
    """
    基于 Reciprocal Rank Fusion (RRF) 的向量 / 图谱结果融合：
      score = sum( 1 / (k + rank_i + 1) )，其中 rank_i 是该 chunk 在该路召回中的名次（0-based）。

    - 向量路召回按 VectorHit 数组顺序作为 rank（Qdrant 已按 cosine 倒序返回）。
    - 图谱路召回按 GraphRelation.hop 升序作为 rank，hop 内再按数组下标递增。
    - 同时被两条路命中的 chunk 会同时拿到两个 1/(k+r+1) 贡献，自然提升其总分。

    该排序与原始 score（向量余弦 / 图谱概率）解耦，因此输出分数落在 (0, 2/(k+1)] 区间。
    """
    k = limits.rrf_k

    # 1) 向量路 rank 映射：Qdrant 已经按相似度倒序返回，所以下标就是 rank。
    vector_rank: dict = {}
    seed_sources_by_chunk: dict = {}
    for idx, hit in enumerate(vector_hits):
        if hit.chunk_id not in vector_rank:
            vector_rank[hit.chunk_id] = idx
            seed_sources_by_chunk[hit.chunk_id] = list(hit.source_chunk_ids or [hit.chunk_id])

    # 2) 图谱路 rank 映射：先按 hop 升序，hop 内按下标递增。
    #    hop=1 的关系更接近种子实体，理应获得更小的 rank（更靠前）。
    ranked_relations = sorted(
        enumerate(graph_result.relations),
        key=lambda x: (x[1].hop, x[0]),
    )
    graph_rank: dict = {}
    extra_sources_by_chunk: dict = {}
    for rank, (_, rel) in enumerate(ranked_relations):
        for chunk_id in rel.source_chunk_ids:
            if chunk_id not in graph_rank:
                graph_rank[chunk_id] = rank
            bucket = extra_sources_by_chunk.get(chunk_id, [])
            for sc in rel.source_chunk_ids:
                if sc not in bucket:
                    bucket.append(sc)
            extra_sources_by_chunk[chunk_id] = bucket

    # 3) 聚合每个 chunk 的贡献。RRF 分数越高越相关。
    all_chunk_ids = set(vector_rank.keys()) | set(graph_rank.keys())
    by_id: dict = {}
    for chunk_id in all_chunk_ids:
        v_rank = vector_rank.get(chunk_id)
        g_rank = graph_rank.get(chunk_id)
        rrf_score = (
            (1.0 / (k + v_rank + 1) if v_rank is not None else 0.0)
            + (1.0 / (k + g_rank + 1) if g_rank is not None else 0.0)
        )

        if v_rank is not None and g_rank is not None:
            via = "vector+graph"
        elif v_rank is not None:
            via = "vector"
        else:
            via = "graph"

        base_sources = list(seed_sources_by_chunk.get(chunk_id, [chunk_id]))
        extras = [
            sc
            for sc in extra_sources_by_chunk.get(chunk_id, [])
            if sc not in base_sources
        ]
        by_id[chunk_id] = CandidateEvidence(
            chunk_id=chunk_id,
            score=rrf_score,
            via=via,  # type: ignore[arg-type]
            source_chunk_ids=base_sources + extras,
        )

    return sorted(
        by_id.values(),
        key=lambda ev: (-ev.score, ev.chunk_id),
    )[: limits.max_candidates]
