"""VLM caption provider — 对应 TS 版 src/caption-provider.ts。

把图片 bytes 转成 data URL，调 OpenAI 兼容 Chat Completions，取回一段简短描述。
供应商兼容百炼（dashscope compatible-mode）、OpenAI gpt-4o-mini、智谱 glm-4v 等。

设计要点：
1. 不绑定任何 SDK，纯 httpx + 超时控制，与 create_llm_graph_extractor 同源；
2. 失败时抛异常；worker 用指数退避重试或归类为 skipped，不让单张图拖垮整库；
3. 输出经 trim + 截断，避免模型塞 markdown / JSON / 长 prefix 让向量检索拿到噪声。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Awaitable, Callable, Optional

import httpx

DEFAULT_CAPTION_SYSTEM_PROMPT = (
    "You caption a single image used as supporting material in a knowledge base. "
    "Write ONE concise paragraph (≤ 80 Chinese characters or ≤ 30 English words). "
    "Focus on: subject, visual attributes (color, shape, texture), and the action / state the image conveys. "
    'Avoid: greetings, preamble, JSON, bullet lists, "the image shows…", markdown fences, and any non-visual speculation.'
)

# caption 输入：图片字节 + MIME + 可选上下文提示
ImageCaptioner = Callable[[dict[str, Any]], Awaitable[Optional[str]]]


def _to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _trim_caption(raw: str, max_chars: int) -> str:
    cleaned = re.sub(r"^```(?:json|text)?\s*|```\s*$", "", raw, flags=re.MULTILINE)
    match = re.match(r'^"(.*)"$', cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(1)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].strip()


def create_openai_compatible_captioner(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_ms: int = 60_000,
    max_chars: int = 240,
    disable_thinking: bool = False,
    client: httpx.AsyncClient | None = None,
) -> ImageCaptioner:
    if not base_url or not base_url.strip():
        raise ValueError("Caption baseUrl is required")
    if not api_key or not api_key.strip():
        raise ValueError("Caption apiKey is required")
    if not model or not model.strip():
        raise ValueError("Caption model is required")

    _client = client or httpx.AsyncClient()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    async def caption(input: dict[str, Any]) -> str | None:
        user_text = f"Context: {input['hint']}" if input.get("hint") else "Describe the image."
        body: dict[str, Any] = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 256,
            "messages": [
                {"role": "system", "content": DEFAULT_CAPTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": _to_data_url(input["bytes"], input["mime"])},
                        },
                    ],
                },
            ],
        }
        # 混合思考模型（qwen3-omni 等）思考 token 计入 max_tokens，会把 caption 正文挤空，
        # 按需注入 enable_thinking:false。
        if disable_thinking:
            body["enable_thinking"] = False

        resp = await _client.post(
            endpoint,
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
            timeout=timeout_ms / 1000,
        )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise RuntimeError(f"caption LLM HTTP {resp.status_code}: {detail}")
        payload = resp.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        if not content:
            return None
        if isinstance(content, list):
            text = "".join(part.get("text") or "" for part in content if isinstance(part, dict))
        else:
            text = str(content)
        cleaned = _trim_caption(text, max_chars)
        return cleaned or None

    return caption


def create_null_captioner() -> ImageCaptioner:
    """默认 captioner：缺省配置时直接返回 None（标记为 skipped，不重试）。"""

    async def _null(input: dict[str, Any]) -> str | None:  # noqa: ARG001
        return None

    return _null
