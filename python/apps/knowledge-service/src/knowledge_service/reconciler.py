"""任务对账 — 对应 TS 版 src/reconciler.ts 与 index.ts 中的对账循环。

原则：数据库任务状态是事实，队列只是事件总线。
- find_missing_index_jobs：DB 里 queued / 租约过期、但队列里已不存在的任务；
- requeue_missing_jobs：以数据库 job ID 幂等补投；
- reconcile_once：单轮对账（指标 + 按需开 span）；
- reconcile_caption_jobs / reconcile_orphan_caption_assets：caption 侧两道对账。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from opentelemetry.trace import StatusCode

# ── 基础对账原语 ──────────────────────────────────────────────────────────


async def find_missing_index_jobs(
    repo: Any,
    queue: Any,
    now: datetime | None = None,
    limit: int = 100,
) -> list[Any]:
    """找出数据库中 queued/stale、但队列里已不存在的任务（需要补偿入队）。"""
    now = now or datetime.now(timezone.utc)
    jobs = await repo.list_queued_or_stale_index_jobs(now, limit)
    missing: list[Any] = []
    for job in jobs:
        existing = await queue.get_job(job["id"]) if callable(getattr(queue, "get_job", None)) else None
        if not existing:
            missing.append(job)
    return missing


async def requeue_missing_jobs(jobs: list[Any], queue: Any) -> int:
    """以数据库 job ID 幂等补投丢失的任务，返回实际入队数量。"""
    count = 0
    for job in jobs:
        await queue.add("index", job, job_id=str(job["id"]))
        count += 1
    return count


async def reconcile_queued_jobs(
    repo: Any,
    queue: Any,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    missing = await find_missing_index_jobs(repo, queue, now, limit)
    return await requeue_missing_jobs(missing, queue)


def _safely(action: Any) -> None:
    try:
        action()
    except Exception:
        pass


# ── 单轮对账（带遥测）─────────────────────────────────────────────────────

async def reconcile_once(repository: Any, queue: Any, telemetry: Any) -> None:
    """对账循环：数据库任务状态是事实，遥测只记录尝试/重新入队数量与最终结果。

    空轮询（绝大多数情况）只记指标用于心跳与失败率，不产生 trace，避免每 30 秒
    向观测后端刷一条空 span；仅在实际补偿入队或对账失败时才开 span。
    """
    started_at = time.monotonic()
    span = None
    outcome = "success"
    requeued = 0
    tracer = getattr(telemetry, "tracer", None) if telemetry else None
    try:
        missing = await find_missing_index_jobs(repository, queue)
        if missing:
            span = tracer.start_span("knowledge.reconcile") if tracer else None
            requeued = await requeue_missing_jobs(missing, queue)
            _safely(lambda: span.set_attribute("requeued", requeued) if span else None)
    except Exception as error:
        outcome = "failure"
        _safely(
            lambda: (
                span.set_status(StatusCode.ERROR) if span else None,
                span.set_attribute(
                    "error.type", type(error).__name__[:40] if isinstance(error, Exception) else "unknown"
                )
                if span
                else None,
                span.record_exception(error) if span and isinstance(error, Exception) else None,
            )
        )
        raise
    finally:
        _safely(
            lambda: (
                telemetry.metrics.knowledge_operation(
                    operation="reconcile",
                    outcome=outcome,
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )
                if telemetry
                else None,
                span.end() if span else None,
            )
        )


# ── caption 侧对账 ────────────────────────────────────────────────────────

async def reconcile_caption_jobs(repository: Any, queue: Any, logger: Any) -> int:
    """扫描数据库中 queued / 租约过期的 caption 任务，重新投递到队列。

    与 index 任务的 reconciler 相同模式：DB 是事实，队列只是事件总线。
    """
    if not callable(getattr(repository, "list_queued_or_stale_caption_jobs", None)):
        return 0
    jobs = await repository.list_queued_or_stale_caption_jobs(datetime.now(timezone.utc), 50)
    requeued = 0
    for job in jobs:
        try:
            await queue.add(
                "caption-asset",
                {"tenantId": str(job["tenant_id"]), "jobId": str(job["id"])},
                job_id=str(job["id"]),
            )
            requeued += 1
        except Exception as error:
            log_warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(log_warn):
                log_warn(
                    "caption reconcile requeue failed",
                    operation="caption-reconcile",
                    job_id=str(job.get("id")),
                    error=str(error),
                )
    return requeued


async def reconcile_orphan_caption_assets(repository: Any, logger: Any) -> int:
    """caption 孤儿对账：caption_status=pending 但无任务行的资产，逐条补一条 caption job。

    复用仓库层的入队逻辑（同样会写 caption_status='queued'），避免单独 SQL 路径
    与正常路径的状态机分叉。
    """
    if not callable(getattr(repository, "list_pending_orphan_caption_assets", None)):
        return 0
    orphans = await repository.list_pending_orphan_caption_assets(50)
    enqueued = 0
    for orphan in orphans:
        try:
            result = await repository.enqueue_caption_job({
                "tenant_id": orphan["tenantId"],
                "kb_id": orphan["kbId"],
                "asset_id": orphan["id"],
            })
            if result:
                enqueued += 1
        except Exception as error:
            log_warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(log_warn):
                log_warn(
                    "orphan caption reconcile failed",
                    operation="orphan-caption-reconcile",
                    asset_id=str(orphan.get("id")),
                    error=str(error),
                )
    return enqueued
