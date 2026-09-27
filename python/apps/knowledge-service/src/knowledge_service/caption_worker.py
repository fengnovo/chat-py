"""Caption worker — 对应 TS 版 src/caption-worker.ts。

BullMQ → arq：队列名保持 "knowledge-caption"，任务函数名 "caption-asset"，
载荷保持 { tenantId, jobId }。

每条任务 = 一张图片（asset_id + 上下文）。流程：
  claim → 拉 bytes → 调 VLM → 写 knowledge_assets.caption → 若已挂 doc 则 reindex。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind, StatusCode

from arq.connections import ArqRedis, RedisSettings
from arq.worker import Worker, func
from observability import extract_observability_context

CAPTION_QUEUE_NAME = "knowledge-caption"


def _safely(action: Any) -> None:
    try:
        action()
    except Exception:
        pass


def _backoff_ms(attempt: int) -> int:
    """计算指数退避：1s, 5s, 30s, 2min, 10min。next_attempt_at 用这个 offset。"""
    table = [1_000, 5_000, 30_000, 120_000, 600_000]
    return table[min(attempt, len(table) - 1)]


async def _process_caption_job(
    ctx: dict[str, Any],
    data: dict[str, Any],
    *,
    deps: Any,
    telemetry: Any,
) -> None:
    """caption 任务主流程。"""
    job_id = data.get("jobId") or ctx.get("job_id")
    tenant_id = data.get("tenantId")

    producer_context = extract_observability_context(data.get("observability") or {})
    producer_span = trace.get_span(producer_context)
    producer_span_context = producer_span.get_span_context() if producer_span else None
    links: list[Link] = []
    if producer_span_context and producer_span_context.is_valid:
        links.append(Link(producer_span_context, {"link.name": "knowledge.caption.enqueue"}))

    # arq 无 job.timestamp / processedOn 字段，用 score（预定执行 ms）推算
    score = ctx.get("score")
    wait_ms: float | None = None
    if isinstance(score, (int, float)):
        wait_ms = max(0.0, time.time() * 1000 - score)
    started_at = time.monotonic()

    tracer = getattr(telemetry, "tracer", None) if telemetry else None
    attributes: dict[str, Any] = {
        "messaging.system": "arq",
        "messaging.destination.name": CAPTION_QUEUE_NAME,
        "messaging.operation.name": "process",
        "job.kind": "caption",
    }
    if job_id:
        attributes["messaging.message.id"] = str(job_id)
    if wait_ms is not None:
        attributes["queue.wait.ms"] = wait_ms

    span = (
        tracer.start_span(
            "knowledge.caption.execute",
            kind=SpanKind.CONSUMER,
            links=links,
            attributes=attributes,
        )
        if tracer
        else None
    )
    if wait_ms is not None and telemetry:
        _safely(lambda: telemetry.metrics.queue_wait(queue=CAPTION_QUEUE_NAME, job="caption", wait_ms=wait_ms))

    claimed: dict[str, str] | None = None
    outcome = "completed"
    try:
        op_started_at = time.monotonic()
        op_outcome = "success"
        try:
            if not tenant_id or not job_id:
                raise ValueError("caption job missing tenantId or jobId")
            lease_ms = deps.lease_ms if hasattr(deps, "lease_ms") else 180_000
            claimed = await deps.repository.claim_caption_job(str(tenant_id), str(job_id), lease_ms)
            if not claimed:
                return
            job_row = await deps.repository.get_caption_job(str(tenant_id), str(job_id))
            if not job_row:
                raise RuntimeError(f"caption job row missing: {job_id}")
            asset = await deps.repository.get_asset_for_caption(str(tenant_id), str(job_row["asset_id"]))
            if not asset:
                await deps.repository.complete_caption_job(
                    str(tenant_id), str(job_id), claimed["lease_token"],
                    {
                        "caption": None, "model": deps.model_name, "status": "skipped",
                        "error": {"code": "asset_missing", "message": "asset deleted or moved"},
                    },
                )
                outcome = "skipped"
                return
            if asset.get("caption_status") == "disabled":
                await deps.repository.complete_caption_job(
                    str(tenant_id), str(job_id), claimed["lease_token"],
                    {"caption": None, "model": deps.model_name, "status": "skipped"},
                )
                outcome = "skipped"
                return
            bytes_data = await deps.download(asset["object_key"])
            caption = await deps.captioner({"bytes": bytes_data, "mime": asset["mime"], "hint": asset["name"]})
            if caption is None:
                # 模型拒答 / 拿到空内容：归类为 skipped，不计入失败重试（已 ready 没意义，跳过即可）。
                await deps.repository.complete_caption_job(
                    str(tenant_id), str(job_id), claimed["lease_token"],
                    {
                        "caption": None, "model": deps.model_name, "status": "skipped",
                        "error": {"code": "empty_response", "message": "captioner returned empty content"},
                    },
                )
                outcome = "skipped"
                return
            await deps.repository.complete_caption_job(
                str(tenant_id), str(job_id), claimed["lease_token"],
                {"caption": caption, "model": deps.model_name, "status": "completed"},
            )
            # 资产已挂到文档则触发再索引，把 caption 嵌入向量空间。
            if asset.get("document_id"):
                try:
                    on_ready = getattr(deps, "on_caption_ready", None)
                    if callable(on_ready):
                        await on_ready({
                            "tenantId": asset["tenant_id"],
                            "kbId": asset["kb_id"],
                            "documentId": asset["document_id"],
                            "assetId": asset["id"],
                        })
                except Exception as error:
                    log_error = getattr(getattr(deps, "logger", None), "error", None)
                    if callable(log_error):
                        log_error("caption ready but reindex trigger failed", asset_id=str(asset["id"]))
        except Exception as error:
            op_outcome = "failure"
            if claimed and tenant_id and job_id:
                try:
                    job_row = await deps.repository.get_caption_job(str(tenant_id), str(job_id))
                    attempts = int(job_row.get("attempts", 1) if job_row else 1)
                    max_attempts = int(job_row.get("max_attempts", 3) if job_row else 3)
                    code = type(error).__name__
                    message = str(error)
                    retry_result = await deps.repository.fail_caption_job_with_retry(
                        str(tenant_id), str(job_id), claimed["lease_token"],
                        {"code": code, "message": message},
                        _backoff_ms(attempts),
                        {"attempts": attempts, "max_attempts": max_attempts},
                    )
                    if retry_result.get("requeued"):
                        outcome = "failed"
                    else:
                        outcome = "skipped"
                        log_warn = getattr(getattr(deps, "logger", None), "warning", None) or getattr(
                            getattr(deps, "logger", None), "warn", None
                        )
                        if callable(log_warn):
                            log_warn("caption retries exhausted, marked skipped", job_id=job_id, code=code, message=message)
                except Exception as inner:
                    outcome = "failed"
                    log_error = getattr(getattr(deps, "logger", None), "error", None)
                    if callable(log_error):
                        log_error("fail_caption_job_with_retry failed", error=str(inner))
            else:
                outcome = "failed"
            raise
        finally:
            _safely(
                lambda: telemetry.metrics.knowledge_operation(
                    operation="caption",
                    outcome=op_outcome,
                    duration_ms=(time.monotonic() - op_started_at) * 1000,
                )
                if telemetry
                else None
            )
    except Exception as error:
        log_error = getattr(getattr(deps, "logger", None), "error", None)
        if callable(log_error):
            log_error("caption job failed", error=str(error))
        _safely(
            lambda: (
                span.set_status(StatusCode.ERROR) if span else None,
                span.set_attribute(
                    "error.type", type(error).__name__[:40] if isinstance(error, Exception) else "unknown"
                )
                if span
                else None,
            )
        )
    finally:
        _safely(
            lambda: (
                telemetry.metrics.queue_job(
                    queue=CAPTION_QUEUE_NAME,
                    job="caption",
                    outcome=outcome,
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )
                if telemetry
                else None,
                span.end() if span else None,
            )
        )


class CaptionConsumerHandle:
    """caption worker 的 close()/is_running() 句柄。"""

    def __init__(self, worker: Worker, task: asyncio.Task) -> None:
        self._worker = worker
        self._task = task

    def is_running(self) -> bool:
        return not self._task.done()

    async def close(self) -> None:
        await _maybe_await(self._worker.close())
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
        except Exception:
            self._task.cancel()


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


def start_caption_worker(
    queue_name: str,
    connection: Any,
    deps: Any,
    concurrency: int = 2,
    telemetry: Any | None = None,
) -> CaptionConsumerHandle:
    """启动 arq worker 消费 caption 队列。"""
    async def handler(ctx: dict[str, Any], data: dict[str, Any]) -> None:
        await _process_caption_job(ctx, data, deps=deps, telemetry=telemetry)

    functions = [func(handler, name="caption-asset")]

    if isinstance(connection, RedisSettings):
        worker_kwargs: dict[str, Any] = {"redis_settings": connection}
    else:
        worker_kwargs = {"redis_pool": connection}

    worker = Worker(
        functions=functions,
        queue_name=queue_name,
        max_jobs=concurrency,
        **worker_kwargs,
    )
    task = asyncio.create_task(worker.async_run())
    return CaptionConsumerHandle(worker, task)


async def find_or_create_caption_job(
    repository: Any,
    input: dict[str, Any],
) -> dict[str, str] | None:
    """简化版 enqueue：API 进程只需把 { tenantId, kbId, assetId } 投到队列。

    jobId 用 knowledge_caption_jobs.id（UUID）—— caption worker 自行拉详情。
    """
    result = await repository.enqueue_caption_job(input)
    return {"id": str(result["id"])} if result else None
