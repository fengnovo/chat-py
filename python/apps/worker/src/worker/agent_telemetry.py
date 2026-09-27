"""Agent 遥测端口映射 — 镜像 apps/worker/src/agent-telemetry.ts。

把 agent-core 的 AgentTelemetry 端口映射到 OTel span/事件与 WorkerMetrics。
run_id/user_id 只进 span attribute；metric label 全部走有限枚举归一。
遥测调用永不影响 agent 执行。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, TypeVar

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .observability import WorkerObservability

T = TypeVar("T")

# 模型名归一到有限 family；未知型号归 other，避免 metric label 基数失控。
def model_family_of(model: str) -> str:
    m = model.lower()
    if "gpt" in m or m.startswith("o") and any(c.isdigit() for c in m[1:]) or "openai" in m:
        return "gpt"
    if "claude" in m:
        return "claude"
    if "gemini" in m:
        return "gemini"
    if "qwen" in m:
        return "qwen"
    return "other"


def _provider_of(provider: str) -> str:
    return provider if provider else "other"


def parse_model_id(id: str) -> tuple[str, str]:
    """ModelSpec.id 形如 `provider:model`；熔断事件只带 model id，需要拆开。"""
    sep = id.find(":")
    if sep <= 0:
        return "other", model_family_of(id)
    return _provider_of(id[:sep]), model_family_of(id[sep + 1 :])


SANDBOX_TOOLS = {
    "execute",
    "write_file",
    "edit_file",
    "read_file",
    "delete",
    "ls",
    "glob",
    "grep",
    "write_todos",
}


def classify_tool(name: str) -> tuple[str, str]:
    """工具名归一到有限枚举；任意自定义/MCP 工具一律归 other。"""
    if name == "graphrag_search":
        return "knowledge", "search"
    if name == "web_search":
        return "web", "search"
    if name == "web_fetch":
        return "web", "retrieve"
    if name in SANDBOX_TOOLS:
        return "sandbox", "execute"
    return "other", "other"


PHASES = {
    "session.lock.acquire",
    "sandbox.acquire",
    "workspace.prepare",
    "agent.resources.upload",
    "agent.runtime.create",
    "agent.execute",
    "persist",
    "cleanup",
    "preparation.parallel",
    "memory.retrieve",
    "memory.reattach",
    "attachments.prepare",
}


def phase_of(operation: str) -> str:
    return operation if operation in PHASES else "other"


def _safely(action: Callable[[], Any]) -> Any:
    """遥测调用永不影响业务。"""
    try:
        return action()
    except Exception:
        return None


class AgentTelemetry:
    """agent-core 的遥测端口实现。"""

    def __init__(self, obs: WorkerObservability) -> None:
        self._tracer = obs.runtime.tracer
        self._metrics = obs.metrics

    def _add_span_event(
        self, name: str, attributes: dict[str, str | int | float | bool] | None = None
    ) -> None:
        def _do() -> None:
            span = trace.get_current_span()
            if span:
                span.add_event(name, attributes or {})

        _safely(_do)

    async def run_span(
        self,
        meta: dict[str, str],
        action: Callable[[], Awaitable[T]],
    ) -> T:
        """以 operation 为名创建 span，失败时记录 exception，结束时上报 phase 指标。"""
        started_at = time.monotonic()
        operation = meta["operation"]
        run_id = meta["run_id"]
        phase = phase_of(operation)
        span = self._tracer.start_span(
            operation,
            attributes={"run_id": run_id, "phase": phase},
        )
        outcome = "success"
        try:
            ctx = trace.set_span_in_context(span)
            from opentelemetry import context as otel_context

            token = otel_context.attach(ctx)
            try:
                return await action()
            finally:
                otel_context.detach(token)
        except Exception as error:
            outcome = "failure"
            _safely(
                lambda: (
                    span.set_status(Status(StatusCode.ERROR)),
                    span.record_exception(error),
                )
            )
            raise
        finally:
            duration_ms = (time.monotonic() - started_at) * 1000
            _safely(
                lambda: self._metrics.agent_phase(
                    phase=phase, outcome=outcome, duration_ms=duration_ms
                )
            )
            _safely(span.end)

    def model_call(self, meta: dict[str, Any]) -> None:
        def _do() -> None:
            family = model_family_of(meta["model"])
            self._metrics.model_call(
                provider=_provider_of(meta["provider"]),
                model=family,
                operation="chat",
                outcome=meta["outcome"],
                duration_ms=max(0.0, float(meta.get("latency_ms", 0))),
                retries=meta.get("retries"),
                fallbacks=meta.get("fallbacks"),
            )
            attrs: dict[str, str | int] = {
                "provider": meta["provider"],
                "model": family,
                "outcome": meta["outcome"],
            }
            if meta.get("retries") is not None:
                attrs["retries"] = meta["retries"]
            if meta.get("fallbacks") is not None:
                attrs["fallbacks"] = meta["fallbacks"]
            self._add_span_event("model.call", attrs)

        _safely(_do)

    def model_tokens(self, meta: dict[str, Any]) -> None:
        def _do() -> None:
            self._metrics.model_tokens(
                provider=_provider_of(meta["provider"]),
                model=model_family_of(meta["model"]),
                input_tokens=meta.get("input_tokens"),
                output_tokens=meta.get("output_tokens"),
            )

        _safely(_do)

    def tool_call(self, meta: dict[str, Any]) -> None:
        def _do() -> None:
            tool, operation = classify_tool(meta["tool"])
            latency = meta.get("latency_ms")
            self._metrics.tool_call(
                tool=tool,
                operation=operation,
                outcome=meta["outcome"],
                duration_ms=max(0.0, float(latency)) if latency is not None else None,
            )
            self._add_span_event(
                "tool.call", {"tool": meta["tool"], "outcome": meta["outcome"]}
            )

        _safely(_do)

    def circuit(self, meta: dict[str, Any]) -> None:
        def _do() -> None:
            provider, model = parse_model_id(meta["model"])
            self._metrics.model_circuit(
                provider=provider, model=model, state=meta["state"]
            )
            self._add_span_event(
                "model.circuit", {"model": meta["model"], "state": meta["state"]}
            )

        _safely(_do)

    def phase(self, meta: dict[str, Any]) -> None:
        def _do() -> None:
            self._metrics.agent_phase(
                phase=phase_of(meta["operation"]),
                outcome=meta["outcome"],
                duration_ms=max(0.0, float(meta["duration_ms"])),
            )

        _safely(_do)

    def event(
        self, name: str, attributes: dict[str, str | int | float | bool] | None = None
    ) -> None:
        def _do() -> None:
            self._add_span_event(name, attributes)
            if name == "run.terminal" and attributes and attributes.get("outcome") == "failed":
                span = trace.get_current_span()
                if span:
                    span.set_status(Status(StatusCode.ERROR))

        _safely(_do)


def create_agent_telemetry(obs: WorkerObservability) -> AgentTelemetry:
    return AgentTelemetry(obs)
