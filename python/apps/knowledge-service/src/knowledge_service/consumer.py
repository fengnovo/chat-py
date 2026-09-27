"""索引任务消费者 — 对应 TS 版 src/consumer.ts。

BullMQ → arq：
- 队列名保持 "knowledge-index"；
- 任务函数名保持 "index" 与 "reindex-document"（与 API / reconciler 的入队方一致）；
- 任务载荷保持 { tenantId, kbId, documentId, observability? } 不变；
- arq 的 _job_id 承担 BullMQ jobId 的幂等语义。

消费流程：claim 数据库租约 → 以数据库当前状态为准组装索引输入 → 跑 IndexPipeline →
挂载同目录资产。DB 是事实，队列只是事件总线。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind, StatusCode

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.jobs import Job
from arq.worker import Worker, func
from observability import extract_observability_context

INDEX_QUEUE_NAME = "knowledge-index"


def _safely(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:
        pass


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


class ArqJobQueue:
    """BullMQ Queue 的 arq 对应物：add(name, data, job_id=...) / get_job(id)。

    保证 reconciler / caption-ready 回投等路径与 TS 版调用形态一致。
    """

    def __init__(self, redis: ArqRedis, queue_name: str) -> None:
        self._redis = redis
        self.name = queue_name

    async def add(
        self,
        name: str,
        data: dict[str, Any],
        *,
        job_id: str | None = None,
        remove_on_complete: Any = None,  # noqa: ARG002 — BullMQ 兼容参数，arq 无对应物
        remove_on_fail: Any = None,  # noqa: ARG002
    ) -> Any:
        # arq 重复 _job_id 返回 None（幂等入队，等同于 BullMQ 同 jobId 去重）
        return await self._redis.enqueue_job(
            name, data, _job_id=str(job_id) if job_id else None, _queue_name=self.name
        )

    async def get_job(self, job_id: str) -> Any | None:
        info = await Job(str(job_id), self._redis).info()
        return info

    async def close(self) -> None:
        await self._redis.close(close_connection_pool=True)


class ConsumerHandle:
    """arq Worker 的运行句柄，对齐 BullMQ Worker 的 close()/isRunning() 用法。"""

    def __init__(self, worker: Worker, task: asyncio.Task, owns_redis: ArqRedis | None = None) -> None:
        self._worker = worker
        self._task = task
        self._owns_redis = owns_redis

    def is_running(self) -> bool:
        return not self._task.done()

    async def close(self) -> None:
        await _maybe_await(self._worker.close())
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
        except Exception:
            self._task.cancel()
        if self._owns_redis is not None:
            await self._owns_redis.close(close_connection_pool=True)


def _extract_ids(data: dict[str, Any]) -> tuple[str, str, str]:
    """任务载荷同时兼容 camelCase（TS 生产路径）与 snake_case。"""
    tenant_id = str(data.get("tenantId") or data.get("tenant_id") or "")
    kb_id = str(data.get("kbId") or data.get("kb_id") or "")
    document_id = str(data.get("documentId") or data.get("document_id") or "")
    return tenant_id, kb_id, document_id


def _queue_wait_ms(ctx: dict[str, Any]) -> float | None:
    """arq 任务的排队等待时长：score（预定执行 epoch ms）→ 开始执行。"""
    score = ctx.get("score")
    if isinstance(score, (int, float)):
        return max(0.0, time.time() * 1000 - score)
    enqueue_time = ctx.get("enqueue_time")
    if isinstance(enqueue_time, datetime):
        now = datetime.now(tz=enqueue_time.tzinfo or timezone.utc)
        return max(0.0, (now - enqueue_time).total_seconds() * 1000)
    return None


async def _process_index_job(ctx: dict[str, Any], data: dict[str, Any], *, deps: Any, telemetry: Any) -> None:
    """索引任务主流程（trace / 指标 / 租约 / pipeline），对应 TS consumer 的 handler。"""
    job_id = ctx.get("job_id")
    # 索引任务由 API 入队时携带 trace 上下文；reconciler 补偿入队的旧任务没有上下文，
    # consumer 作为独立 root trace，用 link 关联上游。
    producer_context = extract_observability_context(data.get("observability") or {})
    producer_span = trace.get_span(producer_context)
    producer_span_context = producer_span.get_span_context() if producer_span else None
    links: list[Link] = []
    if producer_span_context and producer_span_context.is_valid:
        links.append(Link(producer_span_context, {"link.name": "knowledge.enqueue"}))

    wait_ms = _queue_wait_ms(ctx)
    started_at = time.monotonic()

    tracer = getattr(telemetry, "tracer", None) if telemetry else None
    attributes: dict[str, Any] = {
        "messaging.system": "arq",
        "messaging.destination.name": INDEX_QUEUE_NAME,
        "messaging.operation.name": "process",
        "job.kind": "index",
    }
    if job_id:
        attributes["messaging.message.id"] = str(job_id)
    if wait_ms is not None:
        attributes["queue.wait.ms"] = wait_ms

    span = (
        tracer.start_span(
            "knowledge.job.execute",
            kind=SpanKind.CONSUMER,
            links=links,
            attributes=attributes,
        )
        if tracer
        else None
    )
    if wait_ms is not None and telemetry:
        _safely(lambda: telemetry.metrics.queue_wait(queue=INDEX_QUEUE_NAME, job="index", wait_ms=wait_ms))

    claimed: dict[str, str] | None = None
    claimed_once = False
    outcome = "completed"
    try:
        index_started_at = time.monotonic()
        index_outcome = "success"
        try:
            tenant_id, kb_id, document_id = _extract_ids(data)
            claimed = await deps.repository.claim_index_job(
                tenant_id, str(job_id), deps.lease_ms
            )
            if not claimed:
                return
            claimed_once = True

            if callable(getattr(deps.repository, "get_document_for_index", None)):
                # 生产路径：队列里只存任务行（snake_case），文档与切片配置一律以数据库当前状态为准。
                document = await deps.repository.get_document_for_index(tenant_id, kb_id, document_id)
                if not document:
                    raise RuntimeError(f"Index document not found: {document_id}")
                knowledge_base = await deps.repository.get_knowledge_base_for_index(tenant_id, kb_id)
                if not knowledge_base:
                    raise RuntimeError(f"Knowledge base not found: {kb_id}")
                job_input = SimpleNamespace(
                    id=str(job_id),
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    object_key=document["object_key"],
                    content_hash=document["content_hash"],
                    content_encoding=document.get("content_encoding"),
                    size_bytes=int(document["size_bytes"]),
                    mime=document["mime"],
                    chunk_size=int(knowledge_base["chunk_size"]),
                    chunk_overlap=int(knowledge_base["chunk_overlap"]),
                    lease_token=claimed["lease_token"],
                )
            else:
                # host-adapter / 测试路径：调用方在任务里直接内联完整索引参数。
                job_input = SimpleNamespace(
                    **{**data, "id": str(job_id), "tenant_id": tenant_id, "lease_token": claimed["lease_token"]}
                )
            await deps.pipeline.run(job_input)
            # 文档索引完成后，把同目录下尚未挂载的资源绑到本文档，便于检索时按 (document_id, rel_path) 命中。
            if callable(getattr(deps.repository, "attach_assets_to_document", None)):
                _safely(
                    lambda: asyncio.ensure_future(
                        deps.repository.attach_assets_to_document(tenant_id, kb_id, document_id)
                    )
                )
        except Exception:
            index_outcome = "failure"
            raise
        finally:
            # 未抢到租约的任务是正常跳过，不结算 index 指标，避免虚增成功率分母。
            if claimed_once:
                _safely(
                    lambda: telemetry.metrics.knowledge_operation(
                        operation="index",
                        outcome=index_outcome,
                        duration_ms=(time.monotonic() - index_started_at) * 1000,
                    )
                    if telemetry
                    else None
                )
    except Exception as error:
        outcome = "failed"
        if claimed:
            tenant_id = str(data.get("tenantId") or data.get("tenant_id") or "")
            fail = getattr(deps.repository, "fail_index_job", None)
            if callable(fail):
                await fail(tenant_id, str(job_id), claimed["lease_token"], error)
        index_failure = getattr(deps, "metrics", None) and getattr(deps.metrics, "index_failure", None)
        if callable(index_failure):
            index_failure()
        log_error = getattr(getattr(deps, "logger", None), "error", None)
        if callable(log_error):
            log_error("index job failed", error=str(error))
        _safely(
            lambda: (
                span.set_status(StatusCode.ERROR),
                span.set_attribute(
                    "error.type", type(error).__name__[:40] if isinstance(error, Exception) else "unknown"
                ),
            )
            if span
            else None
        )
        raise
    finally:
        _safely(
            lambda: (
                # consume 覆盖整个任务处理（含加载/租约），index 只覆盖索引段。
                telemetry.metrics.knowledge_operation(
                    operation="consume",
                    outcome="success" if outcome == "completed" else "failure",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )
                if claimed_once and telemetry
                else None,
                telemetry.metrics.queue_job(
                    queue=INDEX_QUEUE_NAME,
                    job="index",
                    outcome=outcome,
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )
                if telemetry
                else None,
                span.end() if span else None,
            )
        )


def start_consumer(
    queue_name: str,
    connection: RedisSettings | ArqRedis,
    deps: Any,
    concurrency: int = 2,
    telemetry: Any | None = None,
) -> ConsumerHandle:
    """启动 arq worker 消费索引队列；返回带 close()/is_running() 的句柄。"""
    pipeline = getattr(deps, "pipeline", None)
    if pipeline is None:
        from knowledge_graphrag.indexer import IndexPipeline

        pipeline = IndexPipeline(deps)
        deps.pipeline = pipeline  # type: ignore[attr-defined]

    async def index_handler(ctx: dict[str, Any], data: dict[str, Any]) -> None:
        await _process_index_job(ctx, data, deps=deps, telemetry=telemetry)

    functions = [
        func(index_handler, name="index"),
        func(index_handler, name="reindex-document"),
    ]

    owns_redis: ArqRedis | None = None
    if isinstance(connection, RedisSettings):
        worker_kwargs: dict[str, Any] = {"redis_settings": connection}
    else:
        owns_redis = None  # 外部传入的连接池不在这里关闭
        worker_kwargs = {"redis_pool": connection}

    worker = Worker(
        functions=functions,
        queue_name=queue_name,
        max_jobs=concurrency,
        **worker_kwargs,
    )
    task = asyncio.create_task(worker.async_run())
    return ConsumerHandle(worker, task, owns_redis)


async def create_arq_redis(redis_url: str) -> ArqRedis:
    """从 redis:// URL 创建 arq 连接池（生产侧 enqueue 用）。"""
    return await create_pool(RedisSettings.from_dsn(redis_url))
