"""弹性模型路由：按候选顺序降级 + 指数退避重试 + 熔断。

对应 TS 的 createResilientModelRouter + ResilientModelRouter middleware。
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage

from .circuit_breaker import InMemoryCircuitBreakerStore
from .types import AbortSignal, CircuitBreakerStore, ModelRouterEvent, ModelSpec

# langchain 1.x 的 middleware 基类；旧版本没有该 API 时退化为空基类，
# 图组装层（langgraph create_react_agent）会忽略 middleware 参数。
try:  # pragma: no cover - 取决于安装的 langchain 版本
    from langchain.agents.middleware import AgentMiddleware
except Exception:  # pragma: no cover

    class AgentMiddleware:  # type: ignore[no-redef]
        pass


def _override_request(request: Any, **updates: Any) -> Any:
    """兼容不同版本 ModelRequest 的不可变更新入口。"""
    override = getattr(request, "override", None)
    if callable(override):
        return override(**updates)
    model_copy = getattr(request, "model_copy", None)
    if callable(model_copy):
        return model_copy(update=updates)
    return request


def _to_openai_image_block(block: Any) -> dict[str, Any] | None:
    """
    MCP 工具（如沙箱 read_file 读取 PNG）可能返回图片内容块。mcp-adapters 在
    useStandardContentBlocks 下产出 LangChain 标准块
    `{ type: 'image', source_type: 'base64', data, mime_type }`，旧版/直接透传时
    还可能是 MCP 原始形状 `{ type: 'image', data, mimeType }`。OpenAI 兼容接口
    （DeepSeek 等）只认 `{ type: 'image_url' }`，原样发送会被服务端以
    `unknown variant 'image'` 返回 400，导致整轮 run 失败。
    在模型调用边界统一转成 data URL 形式的 image_url。
    """
    if not isinstance(block, dict):
        return None
    if block.get("type") != "image":
        return None
    # 标准块带 source_type；只处理 base64 图片，URL 等其他来源不转换。
    source_type = block.get("source_type")
    if source_type is not None and source_type != "base64":
        return None
    data = block.get("data")
    if not isinstance(data, str) or not data:
        return None
    mime_type = block.get("mime_type") or block.get("mimeType") or "image/png"
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}


def normalize_image_blocks_for_openai(
    messages: list[BaseMessage] | None,
) -> list[BaseMessage] | None:
    if not isinstance(messages, list):
        return messages
    changed = False
    normalized: list[BaseMessage] = []
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            normalized.append(message)
            continue
        message_changed = False
        next_content: list[Any] = []
        for block in content:
            replacement = _to_openai_image_block(block)
            if replacement is None:
                next_content.append(block)
            else:
                message_changed = True
                next_content.append(replacement)
        if not message_changed:
            normalized.append(message)
            continue
        changed = True
        # model_copy(update=...) 保留原消息类型（ToolMessage 等）及
        # tool_calls / tool_call_id 等全部字段，避免 isinstance 语义丢失。
        normalized.append(message.model_copy(update={"content": next_content}))
    return normalized if changed else messages


@dataclass
class RouterOptions:
    models: list[ModelSpec]
    circuit_breaker: CircuitBreakerStore | None = None
    max_retries: int | None = None
    initial_delay_ms: int | None = None
    max_delay_ms: int | None = None
    on_event: Callable[[ModelRouterEvent], None] | None = None
    telemetry: Any = None  # 仅需 model_call / event 两个方法


@dataclass
class ModelRouter:
    """路由产物：primary 模型实例 + 挂在图上的重试/降级 middleware。"""

    primary: Any
    middleware: Any


@dataclass
class _Candidate:
    spec: ModelSpec
    instance: Any


def _error_message(error: Any) -> str:
    return str(error) if isinstance(error, BaseException) else str(error)


def is_recoverable_model_error(error: Any) -> bool:
    status_raw = getattr(error, "status", None)
    if status_raw is None:
        status_raw = getattr(error, "status_code", None)
    # openai/httpx 异常有时把状态码挂在 response 上。
    if status_raw is None:
        status_raw = getattr(getattr(error, "response", None), "status_code", None)
    try:
        status = int(status_raw) if status_raw is not None else 0
    except (TypeError, ValueError):
        status = 0
    if status in (408, 409, 429) or status >= 500:
        return True
    code = str(getattr(error, "code", "") or "").upper()
    if code in ("ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN"):
        return True
    message = _error_message(error).lower()
    return bool(
        re.search(r"timeout|timed out|connection|rate limit|temporar|unavailable|overloaded", message)
    )


async def _delay(ms: float, signal: AbortSignal | None = None) -> None:
    if signal is not None and signal.aborted:
        raise signal.reason
    await asyncio.sleep(ms / 1000)
    if signal is not None and signal.aborted:
        raise signal.reason


class ResilientModelRouterMiddleware(AgentMiddleware):
    """模型调用边界中间件：图片块归一化 → 熔断筛选 → 重试 → 降级。"""

    def __init__(
        self,
        candidates: list[_Candidate],
        breaker: CircuitBreakerStore,
        max_retries: int,
        initial_delay_ms: int,
        max_delay_ms: int,
        on_event: Callable[[ModelRouterEvent], None] | None,
        telemetry: Any,
    ) -> None:
        super().__init__()
        self._candidates = candidates
        self._breaker = breaker
        self._max_retries = max_retries
        self._initial_delay_ms = initial_delay_ms
        self._max_delay_ms = max_delay_ms
        self._on_event = on_event
        self._telemetry = telemetry

    def _emit_event(self, event: ModelRouterEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)
        telemetry = self._telemetry
        if telemetry is None:
            return
        attributes: dict[str, Any] = {}
        if event.type == "model.fallback":
            attributes = {"from": event.from_model or "", "to": event.to or ""}
        elif event.type == "model.retry":
            attributes = {
                "model": event.model or "",
                "attempt": event.attempt or 0,
                "delay_ms": event.delay_ms or 0,
            }
        try:
            telemetry.event(event.type, attributes)
        except Exception:
            pass

    def _emit_model_call(self, meta: dict[str, Any]) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.model_call(meta)
        except Exception:
            pass

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        # 归一化在重试/降级之外只做一次（转换幂等），覆盖历史消息里所有图片块。
        request_messages = normalize_image_blocks_for_openai(getattr(request, "messages", None))
        primary = self._candidates[0]
        last_error: BaseException | None = None
        last_spec: ModelSpec = primary.spec
        call_started_at = asyncio.get_running_loop().time()
        signal: AbortSignal | None = getattr(getattr(request, "runtime", None), "signal", None)

        for model_index, candidate in enumerate(self._candidates):
            if not await self._breaker.allows(candidate.spec.id):
                continue
            if model_index > 0:
                previous = self._candidates[model_index - 1]
                self._emit_event(
                    ModelRouterEvent(
                        type="model.fallback",
                        **{"from": previous.spec.id if previous else primary.spec.id},
                        to=candidate.spec.id,
                        reason=_error_message(last_error),
                    )
                )

            for attempt in range(1, self._max_retries + 2):
                try:
                    attempt_started_at = asyncio.get_running_loop().time()
                    next_request = _override_request(
                        request,
                        messages=request_messages
                        if request_messages is not None
                        else getattr(request, "messages", None),
                        model=candidate.instance,
                    )
                    response = await handler(next_request)
                    meta: dict[str, Any] = {
                        "provider": candidate.spec.provider,
                        "model": candidate.spec.model,
                        "outcome": "success",
                        "latencyMs": int(
                            (asyncio.get_running_loop().time() - attempt_started_at) * 1000
                        ),
                    }
                    if attempt > 1:
                        meta["retries"] = attempt - 1
                    if model_index > 0:
                        meta["fallbacks"] = model_index
                    self._emit_model_call(meta)
                    await self._breaker.record_success(candidate.spec.id)
                    return response
                except Exception as error:
                    last_error = error
                    last_spec = candidate.spec
                    if not is_recoverable_model_error(error) or attempt > self._max_retries:
                        break
                    base = min(
                        self._initial_delay_ms * 2 ** (attempt - 1), self._max_delay_ms
                    )
                    delay_ms = max(1, round(base * (0.5 + random.random() * 0.5)))
                    self._emit_event(
                        ModelRouterEvent(
                            type="model.retry",
                            model=candidate.spec.id,
                            attempt=attempt,
                            delayMs=delay_ms,
                            reason=_error_message(error),
                        )
                    )
                    await _delay(delay_ms, signal)
            await self._breaker.record_failure(candidate.spec.id)

        # 全部候选/重试耗尽：按最后一个候选结算一次失败，重试与降级次数随调用带上。
        self._emit_model_call(
            {
                "provider": last_spec.provider,
                "model": last_spec.model,
                "outcome": "failure",
                "latencyMs": int((asyncio.get_running_loop().time() - call_started_at) * 1000),
            }
        )
        if last_error is not None:
            raise last_error
        raise RuntimeError("model router exhausted without an error")


async def create_resilient_model_router(options: RouterOptions) -> ModelRouter:
    if len(options.models) == 0:
        raise ValueError("At least one model must be configured.")
    breaker: CircuitBreakerStore = options.circuit_breaker or InMemoryCircuitBreakerStore()
    max_retries = options.max_retries if options.max_retries is not None else 4
    initial_delay_ms = options.initial_delay_ms if options.initial_delay_ms is not None else 500
    max_delay_ms = options.max_delay_ms if options.max_delay_ms is not None else 8_000

    candidates: list[_Candidate] = []
    for spec in options.models:
        kwargs: dict[str, Any] = {
            "model_provider": spec.provider,
            "api_key": spec.api_key,
            "temperature": 1,
            "max_tokens": spec.max_tokens if spec.max_tokens is not None else 16_000,
            # Python SDK 的 timeout 单位是秒；与 TS 的 300_000ms 对齐。
            "timeout": 300,
            "max_retries": 0,
        }
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        # 不传 configurable_fields：带它 init_chat_model 返回 _ConfigurableModel
        # （非 BaseChatModel 实例），deepagents.resolve_model 会误按字符串 spec
        # 走 init_chat_model 而崩溃；且当前没有任何调用方做运行时参数覆盖。
        instance = init_chat_model(spec.model, **kwargs)
        candidates.append(_Candidate(spec=spec, instance=instance))

    primary = candidates[0]
    middleware = ResilientModelRouterMiddleware(
        candidates=candidates,
        breaker=breaker,
        max_retries=max_retries,
        initial_delay_ms=initial_delay_ms,
        max_delay_ms=max_delay_ms,
        on_event=options.on_event,
        telemetry=options.telemetry,
    )
    return ModelRouter(primary=primary.instance, middleware=middleware)
