"""图谱抽取 — 对应 TS 版 src/extract.ts。

通过 OpenAI 兼容的 Chat Completions 接口抽取知识图谱；
失败时降级为空图（记录日志），不让单个 chunk 的 LLM 抖动导致整篇文档索引失败。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Coroutine

import httpx

from knowledge_graphrag.types import GraphExtraction
from knowledge_graphrag.graph import normalize_entity_key

_SYSTEM_PROMPT = (
    "You extract a concise knowledge graph from a single document passage.\n"
    "Return ONLY minified JSON, no markdown, no commentary, with this exact shape:\n"
    '{"entities":[{"name":string,"type":string}],"relationships":[{"source":string,"target":string,"type":string}]}\n'
    "Rules:\n"
    "- Use canonical entity names (proper nouns, concepts); merge aliases case-insensitively.\n"
    '- "type" for relationships is a short verb or predicate (e.g. owns, works_with, located_in).\n'
    "- Relationship source/target must match an entity name exactly.\n"
    "- Extract only facts explicitly supported by the passage; at most 30 entities and 60 relationships."
)

_MAX_ENTITIES = 30
_MAX_RELATIONSHIPS = 60


def _as_trimmed_string(value: Any, max_length: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:max_length]
    return text or None


def _strip_code_fence(raw: str) -> str:
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    return (match.group(1) if match else raw).strip()


def parse_graph_extraction(raw: str) -> GraphExtraction:
    parsed = json.loads(_strip_code_fence(raw))
    entities: list[dict[str, str]] = []
    entity_names: set[str] = set()

    def add_entity(name: str, type_: str = "entity") -> None:
        key = normalize_entity_key(name)
        if key in entity_names:
            return
        entity_names.add(key)
        entities.append({
            "name": name.strip(),
            "key": key,
            "type": (type_.strip()[:80] or "entity"),
        })

    if isinstance(parsed.get("entities"), list):
        for item in parsed["entities"][:_MAX_ENTITIES]:
            if not item or not isinstance(item, dict):
                continue
            name = _as_trimmed_string(item.get("name"))
            if not name:
                continue
            add_entity(name, _as_trimmed_string(item.get("type")) or "entity")

    relationships: list[dict[str, str]] = []
    relation_keys: set[str] = set()
    if isinstance(parsed.get("relationships"), list):
        for item in parsed["relationships"][:_MAX_RELATIONSHIPS]:
            if not item or not isinstance(item, dict):
                continue
            source = _as_trimmed_string(item.get("source"))
            target = _as_trimmed_string(item.get("target"))
            rel_type = _as_trimmed_string(item.get("type")) or _as_trimmed_string(item.get("relation"))
            if not source or not target or not rel_type:
                continue
            # 端点必须存在；模型漏抽时补成默认实体，保证关系可落库
            if normalize_entity_key(source) not in entity_names:
                add_entity(source)
            if normalize_entity_key(target) not in entity_names:
                add_entity(target)
            source_key = normalize_entity_key(source)
            target_key = normalize_entity_key(target)
            dedupe_key = f"{source_key}\x00{rel_type}\x00{target_key}"
            if dedupe_key in relation_keys:
                continue
            relation_keys.add(dedupe_key)
            relationships.append({
                "source": source.strip(),
                "target": target.strip(),
                "type": rel_type.strip(),
                "source_key": source_key,
                "target_key": target_key,
            })

    return GraphExtraction(entities=entities, relationships=relationships)


GraphExtractor = Callable[[str], Coroutine[Any, Any, GraphExtraction]]


def create_llm_graph_extractor(
    *,
    model: str,
    base_url: str,
    api_key: str,
    max_input_chars: int = 8_000,
    timeout_ms: int = 30_000,
    client: httpx.AsyncClient | None = None,
    logger: Any | None = None,
) -> GraphExtractor:
    _client = client or httpx.AsyncClient()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    async def extract(text: str) -> GraphExtraction:
        passage = text.strip()[:max_input_chars]
        if not passage:
            return GraphExtraction(entities=[], relationships=[])
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                resp = await _client.post(
                    endpoint,
                    headers={
                        "content-type": "application/json",
                        "authorization": f"Bearer {api_key}",
                    },
                    json={
                        "model": model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": passage},
                        ],
                    },
                    timeout=timeout_ms / 1000,
                )
                if resp.status_code >= 400:
                    detail = resp.text[:300]
                    raise RuntimeError(f"extraction LLM HTTP {resp.status_code}: {detail}")
                payload = resp.json()
                content = payload.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise RuntimeError("extraction LLM returned empty content")
                return parse_graph_extraction(content)
            except Exception as error:
                last_error = error if isinstance(error, Exception) else RuntimeError(str(error))
        if logger and callable(getattr(logger, "error", None)):
            logger.error(last_error)
        if logger and callable(getattr(logger, "warn", None)):
            logger.warn("graph extraction failed for chunk; continuing without graph edges")
        return GraphExtraction(entities=[], relationships=[])

    return extract
