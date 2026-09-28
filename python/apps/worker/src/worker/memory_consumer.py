"""记忆索引消费者 — 镜像 apps/worker/src/memory-consumer.ts。

提供两个入口：
1. create_memory_queue_processor — 返回 arq 任务函数（job processor），由 arq Worker 调用。
2. start_memory_consumer — 基于轮询的后台任务（用于 arq 以外的独立模式）。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from db import MemoryJobRecord, MemoryRecord
from memory_core import is_sensitive_memory


class _ModelLike(Protocol):
    async def invoke(self, messages: list[dict[str, Any]]) -> Any:
        ...


class _IndexLike(Protocol):
    async def upsert(self, **kwargs: Any) -> None:
        ...


class MemoryRepository(Protocol):
    """扩展 Repository 接口，包含记忆作业管理方法。"""

    async def claim_memory_job(self, lease_ms: int = 300_000) -> MemoryJobRecord | None:
        ...

    async def complete_memory_job(self, id: str) -> None:
        ...

    async def fail_memory_job(
        self, id: str, error: str, delay_ms: int = 30_000
    ) -> None:
        ...

    async def get_run_for_worker(self, tenant_id: str, run_id: str) -> Any:
        ...

    async def list_events_for_worker(self, tenant_id: str, run_id: str) -> list[Any]:
        ...

    async def get_session_for_worker(self, tenant_id: str, session_id: str) -> Any:
        ...

    async def upsert_memory(self, **kwargs: Any) -> Any:
        ...

    async def delete_memory(self, tenant_id: str, user_id: str, id: str) -> None:
        ...


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


def _parse_model_output(value: Any) -> Any:
    raw = value if isinstance(value, str) else _text_of(value)
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    body = fenced.group(1) if fenced else raw
    try:
        return json.loads(body.strip())
    except Exception:
        return []


# 严格约定输出 schema：宽松 prompt 下模型会回 {"operations":[{"key":...,"value":...}]}，
# 导致解析后零写入。这里明确字段名（action/content/normalized_key/...）。
EXTRACT_INSTRUCTION = (
    "Extract durable memories worth remembering in future conversations from the latest exchange. "
    "Return ONLY a JSON array, no prose or code fences. Each element must be: "
    '{"action":"upsert","kind":"fact|preference|profile",'
    '"content":"<one concise factual sentence>",'
    '"normalized_key":"<short stable snake_case key>",'
    '"importance":0.0-1.0,"confidence":0.0-1.0}. '
    'To delete a previously stored memory use {"action":"remove","id":"<memory id>"}. '
    "Ignore greetings, small talk, one-off instructions and transient context. "
    "If nothing is worth storing, return []."
)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_operations(value: Any) -> list[dict[str, Any]]:
    """把模型输出归一化为 [{'action': 'upsert'|'remove', ...}]。

    兼容 {"operations": [...]} 包裹以及 action/op/type、content/text/value、
    normalized_key/key 等常见字段别名。
    """
    if isinstance(value, dict):
        for key in ("operations", "memories", "items", "data"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = []
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    for op in value:
        if not isinstance(op, dict):
            continue
        raw_action = str(
            op.get("action") or op.get("op") or op.get("type") or ""
        ).lower()
        if raw_action in ("remove", "delete"):
            memory_id = str(op.get("id") or op.get("memory_id") or "")
            if memory_id:
                result.append({"action": "remove", "id": memory_id})
            continue
        if raw_action != "upsert":
            continue
        content = op.get("content")
        if content in (None, ""):
            content = op.get("text")
        if content in (None, "") and isinstance(op.get("value"), (dict, list)):
            content = json.dumps(op["value"], ensure_ascii=False)
        elif content in (None, ""):
            content = op.get("value", "")
        result.append(
            {
                "action": "upsert",
                "content": str(content),
                "normalized_key": str(
                    op.get("normalized_key") or op.get("key") or ""
                ).strip(),
                "kind": str(op.get("kind") or "fact"),
                "importance": _as_float(op.get("importance"), 0.7),
                "confidence": _as_float(op.get("confidence"), 0.9),
            }
        )
    return result


def _to_memory_message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


async def process_memory_job(
    repository: MemoryRepository,
    job: MemoryJobRecord,
    model: _ModelLike,
    index: _IndexLike | None = None,
    metrics: Any = None,
) -> None:
    """处理单条记忆提取作业。"""
    tracer = trace.get_tracer("agent-memory")
    span = tracer.start_span(
        "memory.extract",
        attributes={
            "gen_ai.operation.name": "memory.extract",
            "memory.run_id": str(job.run_id),
        },
    )
    try:
        run = await repository.get_run_for_worker(str(job.tenant_id), str(job.run_id))
        if not run or run.status != "completed":
            await repository.complete_memory_job(str(job.id))
            span.set_status(Status(StatusCode.OK))
            return

        events = await repository.list_events_for_worker(
            str(job.tenant_id), str(job.run_id)
        )
        session = await repository.get_session_for_worker(
            str(job.tenant_id), str(job.session_id)
        )
        project_id = session.project_id if session else None
        assistant = "".join(
            e.get("text", "")
            for e in events
            if e.get("type") in ("assistant.delta", "assistant.narration")
        )
        messages = [
            _to_memory_message("user", run.user_message),
            *([_to_memory_message("assistant", assistant)] if assistant else []),
        ]

        extract_started_at = asyncio.get_event_loop().time()

        # 这里使用 memory_core 的 extract_memory_operations（若后续提供），
        # 当前先保留与 TS 一致的调用结构，使用模型直接生成 JSON。
        async def _extract(input: dict[str, Any]) -> Any:
            payload = [
                {
                    "role": "system",
                    "content": f"{input['instruction']} Return JSON only. The user's latest message has priority over stored facts.",
                },
                *input["messages"],
            ]
            # langchain ChatModel：invoke 是同步的（返回 AIMessage），异步入口是 ainvoke。
            if hasattr(model, "ainvoke"):
                response = await model.ainvoke(payload)
            else:
                response = await model.invoke(payload)
            content = response.content if hasattr(response, "content") else response
            return _parse_model_output(content)

        # 直接调用一次模型提取（memory_core 暂无提取器，内联实现与 TS 行为对齐）。
        raw_operations = await _extract(
            {
                "messages": messages,
                "instruction": EXTRACT_INSTRUCTION,
            }
        )
        operations = _normalize_operations(raw_operations)

        if metrics and hasattr(metrics, "memory_operation"):
            metrics.memory_operation(
                operation="extract",
                outcome="success",
                duration_ms=(asyncio.get_event_loop().time() - extract_started_at) * 1000,
            )

        # consolidate_memory_operations 的简化内联实现（与 TS 行为对齐）
        # 待 memory_core 提供 consolidate_memory_operations 后可直接调用。
        for op in operations:
            if op["action"] == "upsert":
                content = op["content"]
                if not content or is_sensitive_memory(content):
                    continue
                upsert_started_at = asyncio.get_event_loop().time()
                saved = await repository.upsert_memory(
                    MemoryRecord(
                        id=str(uuid.uuid4()),
                        tenant_id=str(job.tenant_id),
                        user_id=str(job.user_id),
                        project_id=project_id,
                        assistant_key="chat",
                        scope=f"project:{project_id}" if project_id else "global",
                        kind=op["kind"],
                        content=content[:2_000],
                        normalized_key=op["normalized_key"]
                        or f"manual:{uuid.uuid4().hex[:12]}",
                        importance=op["importance"],
                        confidence=op["confidence"],
                        status="active",
                        source_session_id=str(job.session_id),
                        source_run_id=str(job.run_id),
                        supersedes_id=None,
                        metadata={"extractor": "memory-consumer-v1"},
                    )
                )
                if index:
                    await index.upsert(
                        id=saved.id,
                        tenant_id=saved.tenant_id,
                        user_id=saved.user_id,
                        content=saved.content,
                        normalized_key=saved.normalized_key,
                        kind=saved.kind,
                        importance=saved.importance,
                        confidence=saved.confidence,
                        project_id=saved.project_id,
                        scope=saved.scope,
                    )
                if metrics and hasattr(metrics, "memory_operation"):
                    metrics.memory_operation(
                        operation="upsert",
                        outcome="success",
                        duration_ms=(asyncio.get_event_loop().time() - upsert_started_at) * 1000,
                    )
            else:
                # action == "remove"（归一化后仅保留带 id 的删除操作）
                memory_id = op["id"]
                await repository.delete_memory(
                    str(job.tenant_id), str(job.user_id), memory_id
                )

        await repository.complete_memory_job(str(job.id))
        span.set_status(Status(StatusCode.OK))
    except Exception:
        span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        span.end()


# arq 任务入口（memory queue processor）
_model_router: dict[str, _ModelLike] | None = None
_model_router_lock = asyncio.Lock()


async def _arq_memory_job(
    ctx: dict[str, Any], *_args: Any, **_kwargs: Any
) -> None:
    """arq 任务函数：消费 memory queue。

    载荷被忽略——提取目标由 DB 队列 claim_memory_job() 决定，这样 arq 重试/
    位置或关键字传参（{"runId": ...}）都不会影响消费。
    """
    global _model_router
    repository: MemoryRepository = ctx["repository"]
    index: _IndexLike | None = ctx.get("index")
    metrics: Any = ctx.get("metrics")
    models: list[Any] = ctx["models"]

    # 缓存的是"结果"而非协程——协程只能 await 一次，并发 job=2 时第二个 job
    # 会复用已结束的协程报 cannot reuse already awaited coroutine。
    if _model_router is None:
        async with _model_router_lock:
            if _model_router is None:
                _model_router = await _create_resilient_model_router(models)
    model = _model_router["primary"]

    claimed = await repository.claim_memory_job()
    if not claimed:
        return
    try:
        await process_memory_job(repository, claimed, model, index, metrics)
    except Exception as error:
        await repository.fail_memory_job(
            str(claimed.id),
            str(error) if isinstance(error, Exception) else repr(error),
        )
        raise


async def _create_resilient_model_router(
    models: list[Any],
) -> dict[str, _ModelLike]:
    """构建记忆提取用的主模型路由。

    models 是 worker.config.ModelSpec（snake_case 普通类），需要逐个转成
    agent_core 的 ModelSpec（camelCase 别名、pydantic 模型）。
    """
    try:
        from agent_core import (  # type: ignore[import-not-found]
            ModelSpec as AgentModelSpec,
            RouterOptions,
            create_resilient_model_router,
        )

        specs = [
            AgentModelSpec(
                id=m.id,
                model=m.model,
                provider=m.provider,
                apiKey=m.api_key,
                baseUrl=m.base_url,
                maxTokens=m.max_tokens,
            )
            for m in models
        ]
        router = await create_resilient_model_router(RouterOptions(models=specs))
        return {"primary": router.primary}  # type: ignore[dict-item]
    except Exception as error:
        # 不再静默：真实模型不可用时必须能在日志里看到，否则会"全部成功但零记忆"。
        import logging

        logging.getLogger("worker.memory").warning(
            "create_resilient_model_router failed, falling back to noop model: %r",
            error,
        )

    class _PlaceholderModel:
        def __init__(self, spec: Any) -> None:
            self._spec = spec

        async def invoke(self, messages: list[dict[str, Any]]) -> Any:
            # 占位：返回空 content，让记忆提取不产生错误。
            return {"content": "[]"}

    return {"primary": _PlaceholderModel(models[0] if models else None)}


def create_memory_queue_processor(
    repository: MemoryRepository,
    models: list[Any],
    index: _IndexLike | None = None,
    metrics: Any = None,
) -> Any:
    """返回 arq 可识别的任务函数字典（用于 worker 注册）。

    实际在 arq 中注册为 {'memory_extract': _arq_memory_job} 即可。
    """
    return {
        "repository": repository,
        "models": models,
        "index": index,
        "metrics": metrics,
    }


def start_memory_consumer(
    repository: MemoryRepository,
    models: list[Any],
    index: _IndexLike | None = None,
    metrics: Any = None,
    interval_ms: int = 2_000,
    logger: Any = None,
) -> Any:
    """基于轮询的后台记忆消费者（非 arq 模式时使用）。

    返回 stop 函数；调用后停止轮询。
    """
    stopped = False
    running = False
    model_promise: Any = None

    async def tick() -> None:
        nonlocal running, model_promise
        if stopped or running:
            return
        running = True
        claimed: MemoryJobRecord | None = None
        try:
            job = await repository.claim_memory_job()
            if not job:
                return
            claimed = job
            if model_promise is None:
                model_promise = _create_resilient_model_router(models)
            model_router = await model_promise
            model = model_router["primary"]
            await process_memory_job(repository, job, model, index, metrics)
        except Exception as error:
            if claimed:
                await repository.fail_memory_job(
                    str(claimed.id),
                    str(error) if isinstance(error, Exception) else repr(error),
                )
            if logger and hasattr(logger, "error"):
                logger.error("memory job failed", exc_info=error)
        finally:
            running = False

    async def loop() -> None:
        while not stopped:
            await tick()
            await asyncio.sleep(interval_ms / 1_000)

    task = asyncio.create_task(loop())

    def stop() -> None:
        nonlocal stopped
        stopped = True
        if not task.done():
            task.cancel()

    return stop
