"""agent-core 的类型定义：与 packages/agent-core/src/types.ts 一一对应。

zod schema 在 Python 侧统一替换为 Pydantic 模型；跨模块共享、不含运行逻辑的
协议（Protocol）与数据结构集中在这里，避免各实现文件之间循环依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AbortSignal:
    """TS AbortSignal 的 Python 最小等价物（协作式取消令牌）。

    asyncio 没有内建 AbortSignal；这里提供 aborted / reason / throw_if_aborted /
    事件监听四个语义，供沙箱、子 Agent、模型路由层共用。监听回调为同步函数，
    由持有方（AbortController.abort）在同一事件循环线程内触发。
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason: BaseException = RuntimeError("Aborted")
        self._listeners: list[Callable[[], None]] = []

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> BaseException:
        return self._reason

    def throw_if_aborted(self) -> None:
        if self._aborted:
            raise self._reason

    def add_event_listener(self, callback: Callable[[], None]) -> None:
        # 已中止时立即回调，与 TS addEventListener 配合 AbortSignal.any 的语义一致。
        if self._aborted:
            callback()
            return
        self._listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _abort(self, reason: BaseException | None = None) -> None:
        if self._aborted:
            return
        self._aborted = True
        if reason is not None:
            self._reason = reason
        listeners, self._listeners = self._listeners, []
        for callback in listeners:
            try:
                callback()
            except Exception:
                # 监听器异常不得影响取消本身的传播。
                pass


class AbortController:
    """与 AbortSignal 配对的所有权句柄，对应 TS 的 AbortController。"""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: BaseException | None = None) -> None:
        self.signal._abort(reason)


class ModelSpec(BaseModel):
    """单个候选模型的连接配置。"""

    id: str
    model: str
    provider: str
    api_key: str = Field(alias="apiKey")
    base_url: str | None = Field(default=None, alias="baseUrl")
    # 单次回复的输出上限。推理模型会把思考过程也计入该额度，太小会导致只输出
    # reasoning、正文为空。
    max_tokens: int | None = Field(default=None, alias="maxTokens")

    model_config = ConfigDict(populate_by_name=True)


class ModelRouterEvent(BaseModel):
    """模型路由的可观测事件（重试 / 降级）。"""

    type: Literal["model.retry", "model.fallback"]
    model: str | None = None
    from_model: str | None = Field(default=None, alias="from")
    to: str | None = None
    attempt: int | None = None
    delay_ms: int | None = Field(default=None, alias="delayMs")
    reason: str

    model_config = ConfigDict(populate_by_name=True)


@runtime_checkable
class CircuitBreakerStore(Protocol):
    """熔断器存储端口（依赖倒置）：默认内存实现，也可换成 Redis 等共享存储。"""

    async def allows(self, key: str) -> bool: ...
    async def record_success(self, key: str) -> None: ...
    async def record_failure(self, key: str) -> None: ...


# 熔断器状态有限枚举；任何遥测实现都不得扩展该集合（低基数指标约束）。
CircuitTelemetryState = Literal["open", "half_open", "closed", "rejected"]

AgentTelemetryEvent = Literal[
    "model.retry",
    "model.fallback",
    "retrieval.completed",
    "run.terminal",
]

AgentTelemetryOutcome = Literal["success", "failure"]


@runtime_checkable
class AgentTelemetry(Protocol):
    """Agent 运行时的遥测端口（依赖倒置）。

    agent-core 不依赖 OTel/Langfuse；由 Worker 提供实现。
    约定：实现必须 fail-open——任何方法抛错都不得影响 agent 执行。
    属性只允许 provider/model 标识、工具名、状态、耗时、token 数等低基数字段；
    prompt、completion、工具参数/结果、用户输入一律不得进入。
    """

    async def run_span(
        self,
        meta: dict[str, str],
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        """为一个阶段建立 span 并执行动作；run_id 只进 span attribute，不进 metric label。"""
        ...

    def model_call(self, meta: dict[str, Any]) -> None: ...

    def model_tokens(self, meta: dict[str, Any]) -> None:
        """流式 usage 事件的 token 结算；只计非负数，避免重复/负值结算。"""
        ...

    def tool_call(self, meta: dict[str, Any]) -> None: ...

    def circuit(self, meta: dict[str, Any]) -> None: ...

    def phase(self, meta: dict[str, Any]) -> None: ...

    def event(self, name: AgentTelemetryEvent, attributes: dict[str, Any] | None = None) -> None: ...


# Agent 运行所依赖的沙箱后端类型。
AgentBackendMode = Literal["e2b", "docker"]


class SummarizationConfig(BaseModel):
    """历史消息压缩配置。"""

    # 触发压缩的 token 阈值。
    trigger_tokens: int = Field(alias="triggerTokens")
    # 压缩后保留的最近 token 数（按 token 而非消息条数，因为工具调用单条消息可能很大）。
    keep_tokens: int = Field(alias="keepTokens")
    # 旧消息中截断工具参数的 token 阈值（可选）。
    truncate_args_tokens: int | None = Field(default=None, alias="truncateArgsTokens")

    model_config = ConfigDict(populate_by_name=True)


class KnowledgeMcpOptions(BaseModel):
    """per-run 知识库 MCP 连接配置（携带绑定 run 的短期 JWT，不能跨 run 复用）。"""

    url: str
    token: str
    timeout_ms: int = Field(alias="timeoutMs")
    enabled: bool

    model_config = ConfigDict(populate_by_name=True)


class LongTermMemoryOptions(BaseModel):
    """长期记忆注入配置：store 为 LangGraph BaseStore，remember/forget 为宿主回调。"""

    store: Any
    namespace: list[str]
    profile_path: str | None = Field(default=None, alias="profilePath")
    context: str | None = None
    remember: Callable[..., Awaitable[str]] | None = None
    forget: Callable[[str], Awaitable[bool]] | None = None

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


class HeadlessAgentOptions(BaseModel):
    """create_deep_agent_runtime 的入参。"""

    run_id: str = Field(alias="runId")
    session_id: str = Field(alias="sessionId")
    workspace_path: str = Field(alias="workspacePath")
    backend: Any
    backend_mode: AgentBackendMode | None = Field(default=None, alias="backendMode")
    checkpointer: Any
    models: list[ModelSpec]
    circuit_breaker: CircuitBreakerStore | None = Field(default=None, alias="circuitBreaker")
    mcp_config_path: str | None = Field(default=None, alias="mcpConfigPath")
    knowledge_mcp: KnowledgeMcpOptions | None = Field(default=None, alias="knowledgeMcp")
    skills: list[str] | None = None
    memory: list[str] | None = None
    long_term_memory: LongTermMemoryOptions | None = Field(default=None, alias="longTermMemory")
    auto_approve_tools: bool | None = Field(default=None, alias="autoApproveTools")
    # 单次运行允许的 LangGraph super-step 上限；多步长任务需要足够余量。
    recursion_limit: int | None = Field(default=None, alias="recursionLimit")
    # 单次运行允许的模型调用次数上限。
    model_call_limit: int | None = Field(default=None, alias="modelCallLimit")
    signal: AbortSignal | None = None
    # 遥测端口；缺省时所有观测静默关闭。
    telemetry: AgentTelemetry | None = None
    # 注入到 LangGraph 执行配置的 callbacks（如 Langfuse CallbackHandler）。
    # 由宿主按 run 粒度构建并决定是否采样；agent-core 只负责透传，不识别内容。
    callbacks: list[Any] | None = None
    # 历史消息压缩配置。deepagents 内置的 SummarizationMiddleware 对自定义模型无法
    # 自动推算 trigger，必须由宿主显式提供。传 False 可完全禁用。
    summarization: SummarizationConfig | Literal[False] | None = None

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


class ApprovalResumeInput(BaseModel):
    kind: Literal["approval"] = "approval"
    decision: Literal["approve", "reject"]
    message: str | None = None


class QuestionSelectionInput(BaseModel):
    index: int
    label: str


class QuestionAnswerInput(BaseModel):
    selections: list[QuestionSelectionInput]
    custom_text: str | None = Field(default=None, alias="customText")

    model_config = ConfigDict(populate_by_name=True)


class QuestionResumeInput(BaseModel):
    kind: Literal["question"] = "question"
    answer: QuestionAnswerInput


AgentResumeInput = ApprovalResumeInput | QuestionResumeInput


class ChatImageAttachment(BaseModel):
    """随用户消息一起提交的图片（data URL 内联），用于视觉模型的图文混合输入。"""

    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"] = Field(
        alias="mediaType"
    )
    # 完整 data URL（data:image/...;base64,...）。
    data_url: str = Field(alias="dataUrl")
    filename: str | None = None

    model_config = ConfigDict(populate_by_name=True)


@runtime_checkable
class HeadlessAgentRuntime(Protocol):
    """headless agent 运行句柄：run/resume 以异步生成器流式产出 AgentEvent dict。"""

    backend_mode: AgentBackendMode
    workspace_path: str
    mcp_status: str

    def run(
        self,
        message: str,
        images: list[ChatImageAttachment] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def resume(self, input: AgentResumeInput) -> AsyncIterator[dict[str, Any]]: ...

    async def dispose(self) -> None: ...


# ─── 沙箱基座（直接复用 deepagents 0.7 的 BaseSandbox/响应类型） ───────────
# 切换到官方 deepagents 包后，本地不再自定义 BaseSandbox/ExecuteResponse 等
# 协议类型；E2BSandbox / DockerSandboxBackend 直接继承 deepagents.BaseSandbox，
# 响应也直接返回 deepagents 的 dataclass（字段为 snake_case，与官方一致）。
#
# FileOperationError 仍在本模块定义：deepagents 内部用的是同一组字面量，
# 但本地实现里 _classify_file_error / _to_file_error 返回这些值，外部消费者
# （worker/processor）不直接访问该 Literal 类型，保留本地定义以减少改动面。
FileOperationError = Literal["file_not_found", "is_directory", "permission_denied", "invalid_path"]

try:  # pragma: no cover - 依赖 deepagents 包；切换后才安装
    from deepagents.backends.protocol import (
        ExecuteResponse,
        FileDownloadResponse,
        FileUploadResponse,
    )
    from deepagents.backends.sandbox import BaseSandbox
except Exception as e:  # pragma: no cover - 缺包时给清晰错误
    raise ImportError(
        "agent-core 现在依赖 deepagents 包（>=0.7.19），请先在 python 工作区执行 "
        "`uv add --package agent-core deepagents`"
    ) from e
