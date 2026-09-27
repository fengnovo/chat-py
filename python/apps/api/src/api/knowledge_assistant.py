"""Knowledge assistant helpers — mirrors apps/api/src/knowledge-assistant.ts."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, TypedDict

import httpx
from jose import jwt


class KnowledgeCitationImage(TypedDict, total=False):
    """命中切片关联的知识库资源（图）。assetId 用于换取 api 侧的代理访问地址。"""

    assetId: str
    name: str
    mime: str
    alt: str
    relPath: str


class KnowledgeCitation(TypedDict, total=False):
    chunkId: str
    documentId: str
    documentName: str
    ordinal: int
    heading: str
    score: float
    via: str  # 'vector' | 'graph' | 'both' | ...
    passage: str
    images: list[KnowledgeCitationImage]


def knowledge_asset_content_url(kb_id: str, asset_id: str) -> str:
    """知识库资源（图）在 api 侧的代理访问地址。必须与 web 端 `getKnowledgeAssetContentUrl`
    保持一致：走相对路径，浏览器会自动带上同源 Cookie 完成鉴权。"""
    return f"/api/knowledge-bases/{kb_id}/assets/{asset_id}/content"


MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)>\s]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")

# 追加到各问答提示词的图片输出规则。上下文里的图片地址是 api 代理地址，模型必须原样
# 照抄才能被前端渲染；同时禁止退化成 `./000.jpg` 这类既看不到又点不动的纯文本路径。
IMAGE_ANSWER_RULE = (
    "参考资料中若出现 markdown 图片（形如 ![说明](/api/knowledge-bases/.../assets/.../content)）或「配图」清单，说明该资料确实附有图片。"
    "请把这些图片 markdown 原样写进回答里，地址必须一字不差地照抄（不要加反引号、不要转义、不要删减路径），让用户能直接看到图片。"
    "禁止用 `./000.jpg`、`01.jpeg` 这类相对路径或纯文件名代替图片，也不要声称自己无法发送或展示图片。"
)


def inline_citation_image_urls(
    passage: str,
    images: list[KnowledgeCitationImage] | None,
    kb_id: str,
) -> str:
    """把 passage 里的 markdown 图片（如 `![成品图](./000.jpg)`）替换成可访问的代理地址。
    不改写的话模型只会看到 `./000.jpg` 这种文件系统相对路径，于是复述成纯文本，
    用户既看不到图也点不动。"""
    if not passage or not images:
        return passage
    by_name: dict[str, str] = {}
    for image in images:
        base = image["relPath"].split("/")[-1]
        if base:
            by_name[base.lower()] = knowledge_asset_content_url(kb_id, image["assetId"])
    if not by_name:
        return passage

    def _replace(match: re.Match[str]) -> str:
        alt, src = match.group(1), match.group(2)
        base = src.split("/")[-1].lower()
        url = by_name.get(base)
        if not url:
            return match.group(0)
        return f"![{alt or '配图'}]({url})"

    return MARKDOWN_IMAGE_RE.sub(_replace, passage)


def build_citation_context(citations: list[KnowledgeCitation], kb_id: str) -> str:
    """把命中切片拼成模型上下文。除正文外，为带图切片补一条 markdown 图片清单，
    让模型知道「这条资料附带哪些图、图片地址是什么」，从而直接输出可渲染的图片。"""
    parts: list[str] = []
    for index, citation in enumerate(citations):
        header = f"[{index + 1}] 来源：{citation['documentName']}"
        if citation.get("heading"):
            header += f" / {citation['heading']}"
        passage = inline_citation_image_urls(citation["passage"], citation.get("images"), kb_id)
        # 正文里已内联过的图不再重复列出，避免模型把同一张图输出两遍。
        remaining = [
            image
            for image in citation.get("images", [])
            if knowledge_asset_content_url(kb_id, image["assetId"]) not in passage
        ]
        image_list = ""
        if remaining:
            rendered = " ".join(
                f"![{image.get('alt') or image['name']}]({knowledge_asset_content_url(kb_id, image['assetId'])})"
                for image in remaining
            )
            image_list = f"\n配图：{rendered}"
        parts.append(f"{header}{image_list}\n{passage}")
    return "\n\n".join(parts)


class KnowledgeSearchResult(TypedDict, total=False):
    retrievalId: str
    citations: list[KnowledgeCitation]
    relations: list[dict[str, Any]]
    stats: dict[str, Any]


def sign_run_token(config: Any, context: dict[str, Any]) -> str:
    """签发 knowledge-service 运行令牌（HS256 JWT，5 分钟有效）。"""
    claims = {
        "tenantId": context["tenantId"],
        "userId": context["userId"],
        "sessionId": str(uuid.uuid4()),
        "runId": str(uuid.uuid4()),
        "kbIds": context["kbIds"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "jti": str(uuid.uuid4()),
        "aud": "knowledge-service",
    }
    return jwt.encode(claims, config.secret, algorithm="HS256", headers={"typ": "JWT"})


def parse_mcp_response(text: str) -> Any:
    """MCP Streamable HTTP 响应可能是 SSE（data: 行）或纯 JSON，统一解出 JSON-RPC body。"""
    line = next((item for item in re.split(r"\r?\n", text) if item.startswith("data:")), None)
    payload = line[5:].strip() if line else text.strip()
    return json.loads(payload)


async def mcp_post(
    config: Any,
    token: str,
    body: dict[str, Any],
    session_id: str | None = None,
) -> tuple[Any, str | None]:
    """向 knowledge-service MCP 端点发 POST，返回 (payload, session_id)。"""
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    async with httpx.AsyncClient(timeout=config.timeout_ms / 1000) as client:
        response = await client.post(config.url, headers=headers, json=body)
        text = response.text
        if response.status_code >= 400:
            raise RuntimeError(f"knowledge-service HTTP {response.status_code}: {text[:200]}")
        return parse_mcp_response(text), response.headers.get("mcp-session-id") or session_id


async def search_knowledge(
    config: Any,
    context: dict[str, Any],
    query: str,
    top_k: int | None = None,
) -> KnowledgeSearchResult:
    """通过 knowledge-service 的 graphrag_search 工具执行真实向量+图谱检索。"""
    token = sign_run_token(config, context)
    _, session_id = await mcp_post(
        config,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agent-api-knowledge-console", "version": "1"},
            },
        },
    )
    arguments: dict[str, Any] = {"query": query, "includePassage": True}
    if top_k:
        arguments["topK"] = top_k
    search_payload, _ = await mcp_post(
        config,
        token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "graphrag_search", "arguments": arguments},
        },
        session_id,
    )
    if search_payload.get("error"):
        raise RuntimeError(f"knowledge-service rpc error: {json.dumps(search_payload['error'])}")
    structured = (search_payload.get("result") or {}).get("structuredContent") or {}
    return KnowledgeSearchResult(
        retrievalId=structured.get("retrievalId") or str(uuid.uuid4()),
        citations=structured.get("citations") if isinstance(structured.get("citations"), list) else [],
        relations=structured.get("relations") if isinstance(structured.get("relations"), list) else [],
        stats=structured.get("stats"),
    )


async def answer_with_citations(
    model_config: Any,
    question: str,
    citations: list[KnowledgeCitation],
    kb_id: str | None = None,
) -> str:
    """用检索命中文本作为上下文调用 OpenAI 兼容 Chat Completions，返回带来源编号的回答。"""
    if kb_id:
        context = build_citation_context(citations, kb_id)
    else:
        context = "\n\n".join(
            f"[{i + 1}] 来源：{c['documentName']}\n{c['passage']}" for i, c in enumerate(citations)
        )
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{model_config.base_url.rstrip('/')}/chat/completions",
            headers={
                "authorization": f"Bearer {model_config.api_key}",
                "content-type": "application/json",
            },
            json={
                "model": model_config.model,
                "temperature": 0.3,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是知识库问答助手。只能依据下面提供的参考资料回答问题，使用简体中文。"
                            "若资料不足以回答，请明确说明“根据当前知识库无法回答”。"
                            "回答中引用资料时用 [1]、[2] 这样的编号标注来源，不要编造资料之外的事实。"
                            + IMAGE_ANSWER_RULE
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"参考资料：\n{context}\n\n问题：{question}" if context else f"问题：{question}",
                    },
                ],
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"chat completions HTTP {response.status_code}: {response.text[:200]}"
            )
        payload = response.json()
    content = (
        (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    ).strip()
    return content or "根据当前知识库无法回答。"
