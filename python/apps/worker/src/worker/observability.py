"""Worker 观测面 — 镜像 apps/worker/src/observability.ts。

与 API 共用同一套 OTel SDK，但由 Worker 进程单独建立指标集合与结构化日志。
所有遥测调用 fail-open，任何构造/记录失败都不阻断任务消费。
"""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics as otel_metrics

from observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
    load_observability_config,
)


def _safe_add(counter: Any, amount: int | float, attributes: dict[str, Any]) -> None:
    """指标写入永不影响业务。"""
    try:
        counter.add(amount, attributes)
    except Exception:
        pass


def _safe_record(histogram: Any, value: float, attributes: dict[str, Any]) -> None:
    try:
        histogram.record(value, attributes)
    except Exception:
        pass


class WorkerMetrics:
    """Worker 侧指标集合（镜像 @repo/observability 的 worker 相关指标）。

    label 全部走有限枚举：queue/job/outcome/phase/model family，
    绝不写入 run 原文、用户消息等无界值。
    """

    def __init__(self, meter: otel_metrics.Meter) -> None:
        self._queue_wait = meter.create_histogram(
            "queue.wait",
            unit="ms",
            description="队列内等待时长",
        )
        self._queue_job_duration = meter.create_histogram(
            "queue.job.duration",
            unit="ms",
            description="任务执行时长",
        )
        self._queue_job_counter = meter.create_counter(
            "queue.job",
            description="任务执行次数",
        )
        self._agent_phase_duration = meter.create_histogram(
            "agent.phase.duration",
            unit="ms",
            description="Agent 阶段耗时",
        )
        self._model_call_duration = meter.create_histogram(
            "model.call.duration",
            unit="ms",
            description="模型调用耗时",
        )
        self._model_call_counter = meter.create_counter(
            "model.call",
            description="模型调用次数",
        )
        self._model_tokens = meter.create_counter(
            "model.tokens",
            description="模型 token 消耗",
        )
        self._tool_call_counter = meter.create_counter(
            "tool.call",
            description="工具调用次数",
        )
        self._tool_call_duration = meter.create_histogram(
            "tool.call.duration",
            unit="ms",
            description="工具调用耗时",
        )
        self._model_circuit = meter.create_counter(
            "model.circuit",
            description="模型熔断状态变化",
        )
        self._memory_operation_duration = meter.create_histogram(
            "memory.operation.duration",
            unit="ms",
            description="记忆操作耗时",
        )
        self._memory_operation_counter = meter.create_counter(
            "memory.operation",
            description="记忆操作次数",
        )

    def queue_wait(self, *, queue: str, job: str, wait_ms: float) -> None:
        _safe_record(
            self._queue_wait, wait_ms, {"queue": queue, "job": job}
        )

    def queue_job(
        self, *, queue: str, job: str, outcome: str, duration_ms: float
    ) -> None:
        attrs = {"queue": queue, "job": job, "outcome": outcome}
        _safe_add(self._queue_job_counter, 1, attrs)
        _safe_record(self._queue_job_duration, duration_ms, attrs)

    def agent_phase(self, *, phase: str, outcome: str, duration_ms: float) -> None:
        _safe_record(
            self._agent_phase_duration,
            duration_ms,
            {"phase": phase, "outcome": outcome},
        )

    def model_call(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        outcome: str,
        duration_ms: float,
        retries: int | None = None,
        fallbacks: int | None = None,
    ) -> None:
        attrs: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "operation": operation,
            "outcome": outcome,
        }
        if retries is not None:
            attrs["retries"] = retries
        if fallbacks is not None:
            attrs["fallbacks"] = fallbacks
        _safe_add(self._model_call_counter, 1, attrs)
        _safe_record(self._model_call_duration, duration_ms, attrs)

    def model_tokens(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        attrs = {"provider": provider, "model": model}
        if input_tokens:
            _safe_add(
                self._model_tokens, input_tokens, {**attrs, "token_type": "input"}
            )
        if output_tokens:
            _safe_add(
                self._model_tokens, output_tokens, {**attrs, "token_type": "output"}
            )

    def tool_call(
        self,
        *,
        tool: str,
        operation: str,
        outcome: str,
        duration_ms: float | None = None,
    ) -> None:
        attrs = {"tool": tool, "operation": operation, "outcome": outcome}
        _safe_add(self._tool_call_counter, 1, attrs)
        if duration_ms is not None:
            _safe_record(self._tool_call_duration, duration_ms, attrs)

    def model_circuit(self, *, provider: str, model: str, state: str) -> None:
        _safe_add(
            self._model_circuit,
            1,
            {"provider": provider, "model": model, "state": state},
        )

    def memory_operation(
        self, *, operation: str, outcome: str, duration_ms: float
    ) -> None:
        attrs = {"operation": operation, "outcome": outcome}
        _safe_add(self._memory_operation_counter, 1, attrs)
        _safe_record(self._memory_operation_duration, duration_ms, attrs)


class WorkerObservability:
    """Worker 观测面聚合：config / runtime / metrics / logger。"""

    def __init__(
        self,
        runtime: ObservabilityRuntime,
        *,
        service_version: str,
        shutdown_timeout_ms: int = 5_000,
    ) -> None:
        self.runtime = runtime
        self.config: ObservabilityConfig = load_observability_config(
            service_name="agent-worker",
            service_version=service_version,
        )
        self.shutdown_timeout_ms = shutdown_timeout_ms
        self.metrics = WorkerMetrics(runtime.meter)
        self.logger = runtime.logger


def create_worker_observability(
    runtime: ObservabilityRuntime,
    *,
    service_version: str,
    shutdown_timeout_ms: int = 5_000,
) -> WorkerObservability:
    return WorkerObservability(
        runtime,
        service_version=service_version,
        shutdown_timeout_ms=shutdown_timeout_ms,
    )
