import hashlib
import re
from collections import deque
from typing import List, Set, Dict

from .types import (
    GraphEntity,
    GraphExtraction,
    GraphLimits,
    GraphRelation,
    GraphStore,
    GraphTraversal,
)


def normalize_entity_key(name: str) -> str:
    """实体 key 归一化：去空格、转小写、压缩连续空白"""
    return re.sub(r"\s+", " ", name.strip().lower())


def _relation_id(source: str, rel_type: str, target: str) -> str:
    """生成关系 ID"""
    return hashlib.sha256(f"{source}\0{rel_type}\0{target}".encode()).hexdigest()[:32]


def _validate_limits(limits: GraphLimits) -> None:
    for v in (limits.max_hops, limits.max_fanout, limits.max_relations):
        if not isinstance(v, int) or v < 0:
            raise ValueError("Invalid graph limit")


class InMemoryGraphStore(GraphStore):
    """内存图谱存储实现"""

    def __init__(self):
        # entity_key -> set(chunk_id)
        self._entities: Dict[str, Set[str]] = {}
        # relation_id -> GraphRelation
        self._relations: Dict[str, GraphRelation] = {}
        # document_id -> set(chunk_id)
        self._documents: Dict[str, Set[str]] = {}

    def add_extraction(self, document_id: str, chunk_id: str, extraction: GraphExtraction) -> None:
        for entity in extraction.entities:
            key = normalize_entity_key(entity.key or entity.name)
            self._entities.setdefault(key, set()).add(chunk_id)

        for rel in extraction.relationships:
            source = normalize_entity_key(rel.source)
            target = normalize_entity_key(rel.target)
            rel_type = rel.type.strip()
            rid = _relation_id(source, rel_type, target)
            existing = self._relations.get(rid)
            if existing:
                if chunk_id not in existing.source_chunk_ids:
                    existing.source_chunk_ids.append(chunk_id)
            else:
                self._relations[rid] = GraphRelation(
                    id=rid,
                    source=source,
                    target=target,
                    type=rel_type,
                    source_chunk_ids=[chunk_id],
                    hop=0,
                )

        self._documents.setdefault(document_id, set()).add(chunk_id)

    def entity_keys_for_chunks(self, chunk_ids: List[str]) -> Set[str]:
        wanted = set(chunk_ids)
        result: Set[str] = set()
        for key, chunks in self._entities.items():
            if chunks & wanted:
                result.add(key)
        return result

    def traverse(self, seed_keys: List[str], limits: GraphLimits) -> GraphTraversal:
        _validate_limits(limits)
        seeds = list(dict.fromkeys(normalize_entity_key(k) for k in seed_keys))
        seen: Set[str] = set(seeds)
        selected: List[GraphRelation] = []
        selected_ids: Set[str] = set()
        queue = deque((key, 0) for key in seeds)

        while queue and len(selected) < limits.max_relations:
            current_key, hop = queue.popleft()
            if hop >= limits.max_hops:
                continue
            # 找到与当前实体相连的关系，按 fanout 截断
            adjacent = [
                r
                for r in self._relations.values()
                if r.source == current_key or r.target == current_key
            ][: limits.max_fanout]

            for relation in adjacent:
                if len(selected) >= limits.max_relations:
                    break
                if relation.id not in selected_ids:
                    selected_ids.add(relation.id)
                    selected.append(
                        GraphRelation(
                            id=relation.id,
                            source=relation.source,
                            target=relation.target,
                            type=relation.type,
                            source_chunk_ids=list(relation.source_chunk_ids),
                            hop=hop + 1,
                        )
                    )
                next_key = relation.target if relation.source == current_key else relation.source
                if next_key not in seen:
                    seen.add(next_key)
                    queue.append((next_key, hop + 1))

        chunk_ids = list(dict.fromkeys(c for r in selected for c in r.source_chunk_ids))
        return GraphTraversal(entity_keys=list(seen), relations=selected, chunk_ids=chunk_ids)

    def remove_document(self, document_id: str) -> None:
        chunks = self._documents.pop(document_id, None)
        if not chunks:
            return
        for key in list(self._entities.keys()):
            self._entities[key] -= chunks
            if not self._entities[key]:
                del self._entities[key]
        for rid in list(self._relations.keys()):
            rel = self._relations[rid]
            rel.source_chunk_ids = [c for c in rel.source_chunk_ids if c not in chunks]
            if not rel.source_chunk_ids:
                del self._relations[rid]
