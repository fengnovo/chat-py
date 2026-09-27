"""Knowledge Service 观测面 — 对应 TS 版 src/observability.ts。

- 复用 observability 包的 OTel SDK / 结构化日志 / CoreMetrics；
- 额外补齐 TS 版 CoreMetrics 里 knowledge 用到的 queueWait / queueJob /
  knowledgeOperation 三类指标（低基数标签：queue / job / operation / outcome）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from observability import (
    CoreMetrics,
    ObservabilityRuntime,
    create_observability_logger,
    load_observability_config,
)


class KnowledgeMetrics:
    """knowledge-service 指标集合：CoreMetrics + 队列 / 操作维度指标。"""

    def __init__(self, meter: Any) -> None:
        self._core = CoreMetrics(meter)
        self._queue_wait = meter.create_histogram(
            "queue.wait", unit="ms",
            description="任务在队列中的等待时长",
        )
        self._queue_job_counter = meter.create_counter(
            "queue.jobs", description="队列任务处理次数（按结果）",
        )
        self._queue_job_duration = meter.create_histogram(
            "queue.job.duration", unit="ms", description="队列任务处理时长",
        )
        self._knowledge_op_counter = meter.create_counter(
            "knowledge.operations", description="知识操作次数（按结果）",
        )
        self._knowledge_op_duration = meter.create_histogram(
            "knowledge.operation.duration", unit="ms", description="知识操作时长",
        )

    # ── CoreMetrics 透传 ────────────────────────────────────────────────
    def http_server(
        self, *, method: str, route: str, status: int, outcome: str, duration_ms: float
    ) -> None:
        self._core.http_server(
            method=method, route=route, status=status, outcome=outcome, duration_ms=duration_ms
        )

    # ── 队列 / 知识操作指标 ─────────────────────────────────────────────
    def queue_wait(self, *, queue: str, job: str, wait_ms: float) -> None:
        self._queue_wait.record(wait_ms, {"queue": queue, "job": job})

    def queue_job(self, *, queue: str, job: str, outcome: str, duration_ms: float) -> None:
        attributes = {"queue": queue, "job": job, "outcome": outcome}
        self._queue_job_counter.add(1, attributes)
        self._queue_job_duration.record(duration_ms, attributes)

    def knowledge_operation(self, *, operation: str, outcome: str, duration_ms: float) -> None:
        attributes = {"operation": operation, "outcome": outcome}
        self._knowledge_op_counter.add(1, attributes)
        self._knowledge_op_duration.record(duration_ms, attributes)


@dataclass
class KnowledgeObservability:
    config: Any
    runtime: ObservabilityRuntime
    metrics: KnowledgeMetrics
    logger: Any


def create_knowledge_observability(
    runtime: ObservabilityRuntime,
    *,
    service_version: str,
    service_name: str | None = None,
) -> KnowledgeObservability:
    config = load_observability_config(
        None,
        service_name=service_name or "knowledge-service",
        service_version=service_version,
    )
    metrics = KnowledgeMetrics(runtime.meter)
    logger = create_observability_logger(config.service_name)
    return KnowledgeObservability(config=config, runtime=runtime, metrics=metrics, logger=logger)
