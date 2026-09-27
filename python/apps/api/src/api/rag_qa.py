"""RAG QA service — mirrors apps/api/src/rag-qa.ts."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncGenerator, TypedDict

import httpx

from .knowledge_assistant import (
    IMAGE_ANSWER_RULE,
    KnowledgeCitation,
    build_citation_context,
    search_knowledge,
)


class RagQaInput(TypedDict, total=False):
    question: str
    kbId: str
    tenantId: str
    userId: str
    topK: int | None
    minScore: float | None
    history: list[dict[str, str]] | None


class RagQaStep(TypedDict, total=False):
    type: str  # "step"
    step: str  # "query-analysis" | "query-expansion" | "retrieval" | "source-ranking" | "answer-generation"
    status: str  # "running" | "completed" | "error"
    title: str
    detail: str


class RagQaCitations(TypedDict):
    type: str  # "citations"
    citations: list[KnowledgeCitation]


class RagQaDelta(TypedDict):
    type: str  # "delta"
    delta: str


class RagQaDone(TypedDict):
    type: str  # "done"


class RagQaError(TypedDict, total=False):
    type: str  # "error"
    code: str
    message: str


RagQaChunk = RagQaStep | RagQaCitations | RagQaDelta | RagQaDone | RagQaError


QUERY_REWRITE_PROMPT = """你是多轮对话查询改写助手。请根据「历史对话」和「当前问题」，把当前问题改写成一个完整、独立、适合知识库检索的问题。

规则：
1. 如果当前问题包含指代、省略或依赖上下文的词（如「它」「这个」「怎么做」「多少钱」等），必须结合历史对话补全实体。
2. 如果当前问题本身已经完整，直接返回原问题。
3. 只输出改写后的问题本身，不要解释、不要加引号。"""

QUERY_EXPANSION_PROMPT = """你是知识库检索的查询扩展助手。用户的原始问题可能不够完整或使用了口语化表达，请你从多个角度生成 1-3 个检索式，以便从知识库中召回最相关的文档。

要求：
1. 保留原始问题的核心语义（必须作为第一条）。
2. 补充同义词、专业术语、关键实体替换后的变体。
3. 如果问题是多跳/多条件的，拆成更直接的子查询。
4. 只输出 JSON，不要解释。格式：{"queries": ["检索式1", "检索式2", ...]}"""

ANSWER_SYSTEM_PROMPT = f"""你是专业的智能客服助手，只能依据下方提供的「参考资料」回答用户问题，使用简体中文。

回答规则：
1. 每条事实必须能在参考资料中找到依据，用 [1]、[2] 等编号引用来源。
2. 如果参考资料不足以回答，必须明确说「根据当前知识库无法回答」。
3. 不要编造参考资料之外的事实、价格、日期、政策。
4. 优先给出结论，再补充必要解释；回答要简洁、专业、适合客服场景。
5. 若涉及多个并列要点，使用列表呈现。
6. {IMAGE_ANSWER_RULE}"""


def sse_frame(chunk: RagQaChunk) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def rewrite_query(
    model: Any,
    question: str,
    history: list[dict[str, str]],
) -> str | None:
    if not history:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{model.base_url.rstrip('/')}/chat/completions",
            headers={"authorization": f"Bearer {model.api_key}", "content-type": "application/json"},
            json={
                "model": model.model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": QUERY_REWRITE_PROMPT},
                    *[{"role": h["role"], "content": h["content"]} for h in history[-4:]],
                    {"role": "user", "content": f"当前问题：{question}"},
                ],
            },
        )
    if response.status_code >= 400:
        return None
    payload = response.json()
    rewritten = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    if not rewritten or rewritten == question:
        return None
    return rewritten


async def expand_queries(model: Any, question: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{model.base_url.rstrip('/')}/chat/completions",
            headers={"authorization": f"Bearer {model.api_key}", "content-type": "application/json"},
            json={
                "model": model.model,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": QUERY_EXPANSION_PROMPT},
                    {"role": "user", "content": question},
                ],
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"query expansion failed: HTTP {response.status_code} {response.text[:200]}")
    payload = response.json()
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        parsed = json.loads(match.group(0)) if match else {}
    queries = parsed.get("queries") if isinstance(parsed.get("queries"), list) else [question]
    normalized = queries if question in queries else [question, *queries]
    return normalized[:3]


async def retrieve_multiple_queries(
    mcp: Any,
    input: RagQaInput,
    queries: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for query in queries:
        result = await search_knowledge(
            mcp,
            {"tenantId": input["tenantId"], "userId": input["userId"], "kbIds": [input["kbId"]]},
            query=query,
            top_k=input.get("topK") or 20,
        )
        results.append(result)
    return results


def fuse_with_rrf(results: list[dict[str, Any]]) -> list[KnowledgeCitation]:
    k = 60
    by_chunk_id: dict[str, dict[str, Any]] = {}
    for query_index, result in enumerate(results):
        for rank, citation in enumerate(result.get("citations", [])):
            score = 1 / (k + rank + 1)
            existing = by_chunk_id.get(citation["chunkId"])
            if existing:
                existing["rrfScore"] += score
                if query_index not in existing["fromQueries"]:
                    existing["fromQueries"].append(query_index)
            else:
                by_chunk_id[citation["chunkId"]] = {
                    **citation,
                    "rrfScore": score,
                    "fromQueries": [query_index],
                }
    sorted_chunks = sorted(by_chunk_id.values(), key=lambda c: c["rrfScore"], reverse=True)
    return sorted_chunks[:10]


async def stream_answer(
    model: Any,
    question: str,
    citations: list[KnowledgeCitation],
    history: list[dict[str, str]],
    kb_id: str,
) -> AsyncGenerator[str, None]:
    context = build_citation_context(citations, kb_id)
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        *[{"role": h["role"], "content": h["content"]} for h in history],
        {"role": "user", "content": f"参考资料：\n{context}\n\n问题：{question}" if context else f"问题：{question}"},
    ]
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{model.base_url.rstrip('/')}/chat/completions",
            headers={"authorization": f"Bearer {model.api_key}", "content-type": "application/json"},
            json={"model": model.model, "temperature": 0.3, "stream": True, "messages": messages},
        ) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise RuntimeError(f"answer generation failed: HTTP {response.status_code} {text[:200]}")
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                lines = buffer.split("\n")
                buffer = lines.pop() or ""
                for line in lines:
                    trimmed = line.strip()
                    if not trimmed or trimmed == "data: [DONE]":
                        continue
                    if not trimmed.startswith("data: "):
                        continue
                    try:
                        data = json.loads(trimmed[6:])
                    except json.JSONDecodeError:
                        continue
                    delta = (data.get("choices") or [{}])[0].get("delta", {}).get("content")
                    if isinstance(delta, str) and delta:
                        yield delta


async def run_rag_qa(
    input: RagQaInput,
    mcp: Any,
    model: Any,
) -> AsyncGenerator[RagQaChunk, None]:
    def step(step_name: str, status: str, title: str, detail: str | None = None) -> RagQaStep:
        result: RagQaStep = {"type": "step", "step": step_name, "status": status, "title": title}
        if detail is not None:
            result["detail"] = detail
        return result

    try:
        yield step("query-analysis", "running", "正在理解您的问题")
        search_question = input["question"]
        if input.get("history"):
            rewritten = await rewrite_query(model, input["question"], input["history"])
            if rewritten:
                search_question = rewritten
                yield step("query-analysis", "completed", "已理解问题", f"结合上下文改写为：{rewritten}")
            else:
                yield step("query-analysis", "completed", "已理解问题")
        else:
            yield step("query-analysis", "completed", "已理解问题")

        yield step("query-expansion", "running", "正在扩展检索式")
        expanded_queries: list[str] = []
        try:
            expanded_queries = await expand_queries(model, search_question)
            yield step("query-expansion", "completed", "检索式扩展完成", f"将使用 {len(expanded_queries)} 个检索式并行检索")
        except Exception as e:
            expanded_queries = [input["question"]]
            yield step("query-expansion", "completed", "检索式扩展完成", f"扩展失败，使用原问题检索：{e}")

        yield step("retrieval", "running", "正在检索相关知识", " / ".join(expanded_queries))
        raw_results = await retrieve_multiple_queries(mcp, input, expanded_queries)
        total_citations = sum(len(r.get("citations", [])) for r in raw_results)
        yield step("retrieval", "completed", "检索完成", f"召回 {total_citations} 条候选")

        yield step("source-ranking", "running", "正在评估资料相关性")
        ranked = fuse_with_rrf(raw_results)
        filtered = [c for c in ranked if c.get("score", 0) >= input["minScore"]] if input.get("minScore") is not None else ranked
        yield {"type": "citations", "citations": [{k: v for k, v in c.items() if k not in ("rrfScore", "fromQueries")} for c in filtered]}
        yield step("source-ranking", "completed", "资料排序完成", f"融合后保留 {len(filtered)} 条最相关来源")

        yield step("answer-generation", "running", "正在生成回答")
        async for delta in stream_answer(model, input["question"], filtered, input.get("history") or [], input["kbId"]):
            yield {"type": "delta", "delta": delta}
        yield step("answer-generation", "completed", "回答已生成")
    except Exception as e:
        yield {"type": "error", "code": "RAG_QA_FAILED", "message": str(e)}


class RagQaService:
    def __init__(self, config: Any) -> None:
        self.mcp = config.knowledge_mcp
        self.model = config.knowledge_qa_model

    def is_available(self) -> bool:
        return bool(self.mcp and self.model)

    async def stream(self, input: RagQaInput) -> AsyncGenerator[str, None]:
        if not self.mcp or not self.model:
            yield sse_frame({"type": "error", "code": "RAG_NOT_CONFIGURED", "message": "Knowledge MCP or QA model is not configured"})
            return
        async for chunk in run_rag_qa(input, self.mcp, self.model):
            yield sse_frame(chunk)
        yield sse_frame({"type": "done"})

    async def answer(self, input: RagQaInput) -> dict[str, Any]:
        if not self.mcp or not self.model:
            raise RuntimeError("RAG QA service is not configured")
        chunks: list[RagQaChunk] = []
        async for chunk in run_rag_qa(input, self.mcp, self.model):
            chunks.append(chunk)
        answer = "".join(c["delta"] for c in chunks if c["type"] == "delta")
        citations = next((c["citations"] for c in chunks if c["type"] == "citations"), [])
        return {"answer": answer, "citations": citations}


def create_rag_qa_service(config: Any) -> RagQaService:
    return RagQaService(config)
