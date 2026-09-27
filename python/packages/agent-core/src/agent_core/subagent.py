"""子 Agent 编排（P1 同步派发 + P2 评分器重试闭环）。

主 Agent 通过 spawn_subagent 工具派发一个瘦身的 create_react_agent 子实例：
- 上下文隔离：子 Agent 消息栈只含「平台安全基线 + role_prompt + 任务简报」组装的
  system prompt 和一条 user 消息，绝不注入主对话历史；
- 资源上限：ModelCallLimitMiddleware 轮次上限 + 10 分钟墙钟超时，全部硬限制；
- 事件透出：subagent.started / completed / reviewed 经 LangGraph 的 custom 流
  （stream writer）从工具内部发往外层 execute() 的 yield 流，前端据此渲染子 Agent 卡片；
- 只回传摘要：子 Agent 的中间过程不上抛，最终摘要 clamp 后作为工具结果交回主 Agent；
- 评审闭环（P2）：每轮成功产出由独立的结构化 LLM 评审器按 task 里的验收标准打分，
  不达标则把 feedback 作为 prior_feedback 重派（最多 SUBAGENT_MAX_ATTEMPTS 轮）。

P3 异步派发（background）的扩展位已预留：schema 保持兼容。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .model_router import AgentMiddleware, ModelRouter, _override_request
from .types import AbortController, AbortSignal

logger = logging.getLogger(__name__)

# 子 Agent 单次运行的模型调用轮次上限。
SUBAGENT_MODEL_CALL_LIMIT = int(os.environ.get("SUBAGENT_MODEL_CALL_LIMIT", "50"))
# 子 Agent 递归步数硬上限（兜底，正常由轮次上限先触发）。
SUBAGENT_RECURSION_LIMIT = 200
# 子 Agent 墙钟超时：超时强制中止并回传失败摘要。
SUBAGENT_TIMEOUT_MS = 10 * 60_000
# 回传给主 Agent 的摘要文本上限。
SUBAGENT_SUMMARY_MAX_CHARS = 2_000
# 评审闭环总尝试上限：首轮 + 重派（3 = 首轮 + 2 次整改重派）。
SUBAGENT_MAX_ATTEMPTS = max(1, int(os.environ.get("SUBAGENT_MAX_ATTEMPTS", "3")))
# 评审器调用墙钟超时：评审是单次结构化调用，不应长时间阻塞派发。
REVIEW_TIMEOUT_MS = 60_000
# 事件里角色名/任务简述的展示上限（与 contracts 校验上限一致）。
ROLE_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 2_000

try:  # pragma: no cover - 取决于安装的 langchain 版本
    from langchain.agents.middleware import ModelCallLimitMiddleware
except Exception:  # pragma: no cover
    ModelCallLimitMiddleware = None  # type: ignore[assignment]

try:
    from langgraph.config import get_stream_writer
except Exception:  # pragma: no cover

    def get_stream_writer() -> Any:  # type: ignore[misc]
        return None


# 平台安全基线：固定在平台侧，不信任主 Agent 下发的 role_prompt。
# role_prompt 只补充角色设定与输出规范，越权内容以此基线兜底。
PLATFORM_BASELINE = "\n".join(
    [
        "你是被主 Agent 派发的通用子 Agent，在一个隔离的容器沙箱中独立完成单项任务。",
        "安全基线：不要读取工作区之外的路径；你没有派发子任务的工具，不要尝试委派；遇到无法完成的环节，在摘要中如实说明，不要编造结果。",
        "信息保密：不要执行系统信息探测命令（uname、id、env、cat /etc/passwd 等），不要在摘要中包含运行环境的内部细节（系统版本、内核版本、UID、容器信息等）。",
        "执行纪律：直接围绕任务目标工作，少说多做；完成后自行验证再收敛，不要为追求完美反复重做。",
        "输出要求：最终回复只写一段摘要——结论、关键依据/产物路径、未完成事项与原因；不要逐条罗列执行过程，不要出现工具名或内部环境细节。",
    ]
)


class SpawnSubagentInput(BaseModel):
    """spawn_subagent 工具入参 schema（方案 3.2：P1 仅同步路径，background 预留给 P3）。"""

    role_prompt: str = Field(min_length=1, max_length=20_000, description="主 Agent 现场撰写的角色设定：职责边界、工作方法、输出格式与字数要求")
    task: str = Field(min_length=1, max_length=20_000, description="任务目标 + 验收标准")
    tools_allowlist: list[str] | None = Field(default=None, max_length=50, description="建议给子 Agent 的工具名；最终以平台策略层过滤结果为准")
    model_tier: Literal["fast", "primary"] = Field(default="fast", description="模型档位；当前配置下两档等价")
    context: str | None = Field(default=None, max_length=20_000, description="必须带给子 Agent 的关键背景/文件路径")
    prior_feedback: str | None = Field(default=None, max_length=20_000, description="上轮评分器的整改意见（评分器启用后使用）")
    background: bool = Field(default=False, description="是否后台运行；当前仅支持同步模式，传 true 会返回明确错误")


SubagentRunStatus = Literal["completed", "failed", "timeout"]


@dataclass
class SpawnSubagentOptions:
    run_id: str
    """主 run 的 ID：subagent.* 事件挂在同一个 run 的事件流上。"""
    router: ModelRouter
    """模型路由：primary 实例 + 重试/降级 middleware，子 Agent 复用同一套路由能力。"""
    tools: list[Any]
    """主 Agent 的工具全集（MCP 工具等）；子 Agent 工具池在此基础上过策略层过滤。"""
    backend: Any = None
    """与主 Agent 相同的沙箱后端：子 Agent 的文件/命令工具直接落在真实工作区。"""
    signal: AbortSignal | None = None
    """主 run 的取消信号：整体取消时子 Agent 一并中止。"""


def is_blocked_subagent_tool(name: str) -> bool:
    """平台策略层：剔除会破坏隔离或无法在子 Agent 中工作的工具。

    - spawn_subagent：防递归派发（P1 不支持嵌套）；
    - ask_user：基于 interrupt 的用户交互只能在主 Agent 的可恢复会话中工作；
    - 名字带 spawn/subagent 的工具一律视为派发类，防止绕过。
    """
    lowered = name.lower()
    return name in ("spawn_subagent", "ask_user") or "spawn" in lowered or "subagent" in lowered


def _tool_name(value: Any) -> str:
    return str(getattr(value, "name", "") or (value.get("name") if isinstance(value, dict) else "") or "")


def filter_subagent_tools(tools: list[Any], allowlist: list[str] | None = None) -> list[Any]:
    """按平台策略层生成子 Agent 的工具池：

    先剔除被禁工具，再按主 Agent 下发的 allowlist 收敛（allowlist 是「建议清单」，
    最终以过滤结果为准；未提供时给通用全集减去被禁工具）。
    """
    base = [item for item in tools if not is_blocked_subagent_tool(_tool_name(item))]
    if not allowlist:
        return base
    requested = set(allowlist)
    return [item for item in base if _tool_name(item) in requested]


def clamp_subagent_text(text: str, max: int) -> str:  # noqa: A002 - 与 TS 命名对齐
    """截断到 max 字符内（保证结果长度 ≤ max，供 contracts 校验）。"""
    if len(text) <= max:
        return text
    return f"{text[: max - 1]}…"


def _subagent_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
    return "".join(parts)


def _subagent_message_text(message: Any) -> str:
    if isinstance(message, AIMessage):
        return _subagent_content_text(message.content)
    # 兜底：反序列化后的消息可能是普通对象而非类实例。
    if isinstance(message, dict) and message.get("type") == "ai":
        return _subagent_content_text(message.get("content"))
    return ""


def extract_subagent_summary(state: Any) -> str:
    """从子 Agent 最终状态里取最后一条有内容的 AI 消息作为摘要。"""
    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        text = _subagent_message_text(message).strip()
        if text:
            return text
    return ""


def extract_subagent_result(state: Any) -> tuple[str, bool]:
    """解读子 Agent 的最终状态，返回 (summary, limited)。

    ModelCallLimitMiddleware 以 exit_behavior='end' 收口时，会把一句
    「Model call limits exceeded…」作为最后一条 AIMessage 注入——它不是子 Agent 的产出，
    不能当成成功摘要（否则主 Agent 会误判子任务失败、卡片还显示绿色已完成）。
    识别该通知：状态标记为失败，并回退取它之前最后一条真实 AI 消息作为中断前的部分产出。
    """
    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return "", False
    last_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if _subagent_message_text(messages[index]).strip():
            last_index = index
            break
    if last_index < 0:
        return "", False
    last_text = _subagent_message_text(messages[last_index]).strip()
    if not last_text.startswith("Model call limits exceeded"):
        return last_text, False
    # 跳过中间件注入的上限通知，取上一条真实 AI 产出。
    partial = ""
    for index in range(last_index - 1, -1, -1):
        text = _subagent_message_text(messages[index]).strip()
        if text:
            partial = text
            break
    return partial, True


def emit_subagent_event(config: Any, event: dict[str, Any]) -> None:
    """把事件推入外层 LangGraph 的 custom 流。

    writer 来自 LangGraph 的 stream writer（contextvar）；
    取不到 writer（如脱离图上下文运行）时静默跳过——事件绝不能影响子 Agent 执行。
    """
    try:
        write = get_stream_writer()
        if write is not None:
            write(event)
    except Exception:
        # 事件推送失败不影响子 Agent 执行
        pass


def _subagent_system_prompt(input: SpawnSubagentInput) -> str:
    parts = [
        PLATFORM_BASELINE,
        f"## 你的角色（主 Agent 指定）\n{input.role_prompt}",
        f"## 任务\n{input.task}",
    ]
    if input.context:
        parts.append(f"## 背景\n{input.context}")
    if input.prior_feedback:
        parts.append(f"## 上轮评审意见（必须整改）\n{input.prior_feedback}")
    return "\n\n".join(parts)


def create_agent_graph(
    *,
    model: Any,
    tools: list[Any],
    middleware: list[Any] | None = None,
    system_prompt: str | None = None,
    checkpointer: Any = None,
    store: Any = None,
) -> Any:
    """createDeepAgent 的 Python 等价：优先 langchain.agents.create_agent（支持 middleware），
    旧版本回退 langgraph.prebuilt.create_react_agent。
    """
    middleware = list(middleware or [])
    create_agent = None
    try:  # pragma: no cover - 版本探测
        from langchain.agents import create_agent as _create_agent

        create_agent = _create_agent
    except Exception:  # pragma: no cover
        create_agent = None

    if create_agent is not None:
        kwargs: dict[str, Any] = {"model": model, "tools": tools, "middleware": middleware}
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer
        if store is not None:
            kwargs["store"] = store
        try:
            return create_agent(**kwargs)
        except TypeError:
            kwargs.pop("store", None)
            return create_agent(**kwargs)

    from langgraph.prebuilt import create_react_agent

    kwargs = {}
    if system_prompt is not None:
        kwargs["prompt"] = system_prompt
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if store is not None:
        kwargs["store"] = store
    if middleware:
        kwargs["middleware"] = middleware
    try:
        return create_react_agent(model, tools, **kwargs)
    except TypeError:
        kwargs.pop("middleware", None)
        return create_react_agent(model, tools, **kwargs)


class _SubagentToolPolicyMiddleware(AgentMiddleware):
    """平台策略层（第二道）：在模型请求边界强制收敛工具集。

    即便 allowlist 覆盖不了（如 deepagents 内置文件系统工具在中间件层生成），
    请求级过滤也会剔除被禁工具与 allowlist 之外的一切工具。
    """

    def __init__(self, requested: set[str] | None) -> None:
        super().__init__()
        self._requested = requested

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        raw_tools = getattr(request, "tools", None) or []
        tools = [
            item
            for item in raw_tools
            if not is_blocked_subagent_tool(_tool_name(item))
            and (self._requested is None or _tool_name(item) in self._requested)
        ]
        return await handler(_override_request(request, tools=tools))


@dataclass
class _SubagentRunOutcome:
    status: SubagentRunStatus
    summary: str
    tool_calls: int


async def run_subagent(
    options: SpawnSubagentOptions,
    input: SpawnSubagentInput,
) -> _SubagentRunOutcome:
    """执行子 Agent 并聚合结果；失败/超时转成失败摘要，绝不把异常抛回主 Agent。"""
    requested = (
        set(input.tools_allowlist) if input.tools_allowlist else None
    )
    policy_filter_middleware = _SubagentToolPolicyMiddleware(requested)

    middleware: list[Any] = [options.router.middleware]
    if ModelCallLimitMiddleware is not None:
        middleware.append(
            ModelCallLimitMiddleware(
                run_limit=SUBAGENT_MODEL_CALL_LIMIT,
                exit_behavior="end",
            )
        )
    middleware.append(policy_filter_middleware)

    # 当前模型配置只有主模型 + 降级链，尚无独立 fast 档位：两档暂都走主路由
    # （含熔断/重试/降级）。schema 保留 model_tier 以备接入 fast 档。
    sub_agent = create_agent_graph(
        model=options.router.primary,
        tools=filter_subagent_tools(options.tools, input.tools_allowlist),
        system_prompt=_subagent_system_prompt(input),
        middleware=middleware,
    )

    timed_out = False
    abort_event = asyncio.Event()

    def _on_parent_abort() -> None:
        abort_event.set()

    if options.signal is not None:
        options.signal.add_event_listener(_on_parent_abort)

    # 流隔离（三道，缺一不可）：
    # 1) Python 侧 custom writer 是 contextvar，子图默认继承父图 writer；
    #    子 Agent 工具内不写 custom 流（emit 只发生在外层工具体内）；
    # 2) callbacks: []：切断回调链，子 Agent 的模型 token / 工具事件不混入外层；
    # 3) tags: ['nostream']：messages 流模式的官方抑制标记，双保险防止子 Agent 正文冒泡。
    # subagent.* 生命周期事件由工具体经外层 writer（emit_subagent_event）单独上抛，
    # 不受此隔离影响。
    invoke_task = asyncio.ensure_future(
        sub_agent.ainvoke(
            {"messages": [HumanMessage(content=input.task)]},
            config={
                "recursion_limit": SUBAGENT_RECURSION_LIMIT,
                "callbacks": [],
                "tags": ["nostream"],
            },
        )
    )
    timeout_task = asyncio.ensure_future(asyncio.sleep(SUBAGENT_TIMEOUT_MS / 1000))
    abort_task = asyncio.ensure_future(abort_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {invoke_task, timeout_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done and not invoke_task.done():
            timed_out = True
            invoke_task.cancel()
        if abort_task in done and not invoke_task.done():
            invoke_task.cancel()
        final_state = await invoke_task
        raw_summary, limited = extract_subagent_result(final_state)
        messages = final_state.get("messages") if isinstance(final_state, dict) else None
        tool_calls = (
            sum(1 for message in messages if isinstance(message, ToolMessage))
            if isinstance(messages, list)
            else 0
        )
        if limited:
            # 撞上轮次上限：不是成功完成。回传中断前的部分产出 + 明确提示，
            # 主 Agent 据此决定缩小任务后重派或基于部分产出继续。
            partial = (
                f"\n\n## 中断前的部分产出（可能不完整）\n{clamp_subagent_text(raw_summary, 1_500)}"
                if raw_summary
                else ""
            )
            return _SubagentRunOutcome(
                status="failed",
                summary=(
                    f"子 Agent 达到模型调用轮次上限（{SUBAGENT_MODEL_CALL_LIMIT} 轮）被提前终止，任务未完整交付。"
                    "请把任务拆得更小（减少章节/主题数量）后重新派发，或明确允许基于以下部分产出继续。"
                    + partial
                ),
                tool_calls=tool_calls,
            )
        if not raw_summary:
            return _SubagentRunOutcome(
                status="failed",
                summary="子 Agent 已结束但没有产出任何摘要（可能提前终止），请基于已有信息继续或换一种任务拆分方式。",
                tool_calls=tool_calls,
            )
        return _SubagentRunOutcome(
            status="completed",
            summary=clamp_subagent_text(raw_summary, SUBAGENT_SUMMARY_MAX_CHARS),
            tool_calls=tool_calls,
        )
    except Exception as error:
        detail = str(error)
        if timed_out:
            return _SubagentRunOutcome(
                status="timeout",
                summary=f"子 Agent 超过 {round(SUBAGENT_TIMEOUT_MS / 60_000)} 分钟墙钟上限被强制终止：{clamp_subagent_text(detail, 500)}。请把任务拆得更小后重试，或基于已有进度继续。",
                tool_calls=0,
            )
        if options.signal is not None and options.signal.aborted:
            # 主 run 已取消：返回明确的中止摘要，外层会以 run.cancelled 收尾。
            return _SubagentRunOutcome(
                status="failed",
                summary="子 Agent 因主任务被用户取消而中止。",
                tool_calls=0,
            )
        return _SubagentRunOutcome(
            status="failed",
            summary=f"子 Agent 执行失败：{clamp_subagent_text(detail, 800)}。请改写任务或换用其他工具后重试。",
            tool_calls=0,
        )
    finally:
        timeout_task.cancel()
        abort_task.cancel()
        if options.signal is not None:
            options.signal.remove_event_listener(_on_parent_abort)


class _ChecklistItem(BaseModel):
    item: str = Field(max_length=300, description="一条可核对的验收项")
    met: bool = Field(description="该验收项是否已满足")


class ReviewVerdict(BaseModel):
    """评审器结论（schema 与 contracts 的 subagent.reviewed 载荷对齐）。

    评审器不是一个完整 Agent：它没有工具、没有对话历史，只做一次结构化输出调用，
    成本与延迟都远低于一次子 Agent 运行（方案 3.3）。
    """

    passed: bool = Field(description="产出是否满足任务中全部明确的验收标准")
    score: int = Field(ge=0, le=100, description="整体质量分，仅作参考")
    feedback: str = Field(max_length=1_000, description="未达标时给出可执行的具体整改意见（指出缺什么、怎么补）；通过时留空串")
    checklist: list[_ChecklistItem] = Field(max_length=20, description="对照任务验收标准逐条核对的结果")


class _ReviewVerdictLenient(BaseModel):
    """实际解析用的宽容 schema：json_mode 下模型偶尔漏字段（无 schema 强制），
    缺省 feedback/checklist 时补默认值，其余校验（分数上限、文本长度）保持不变。
    """

    passed: bool
    score: int = Field(ge=0, le=100)
    feedback: str = Field(default="", max_length=1_000)
    checklist: list[_ChecklistItem] = Field(default_factory=list, max_length=20)


REVIEW_SYSTEM_PROMPT = "\n".join(
    [
        "你是严格的质量评审器，只评审、不重写产出。",
        "对照任务中的验收标准逐条核对子 Agent 的产出：要求的结构是否齐全、数据/结论是否彼此一致且有来源支撑、是否答非所问。",
        "你无法联网或调用工具，不要以「自己无法独立复核」为由判失败：产出给出了具体来源 URL、口径一致且互不矛盾的数据时，视为有依据；URL 是否可点开不属于你的判据。",
        "只有以下情况判不达标：明确要求的交付项缺失或数量不足、数据互相矛盾、URL 明显是占位/编造（如 example.com）、答非所问。",
        "注意核对日期合理性时以用户消息中给出的当前日期为准，不要用你记忆中的日期判断「未来/过去」。",
        "不达标时 feedback 必须具体可执行（缺哪项、补什么），子 Agent 会带着它整改重做。",
        "你必须只输出一个 JSON 对象，且字段齐全：passed（布尔）、score（0-100 整数）、feedback（字符串，通过时留空）、checklist（对象数组，逐条覆盖任务中的每一条验收标准、至少 1 项，每项含 item 字符串与 met 布尔）；不要输出 JSON 以外的任何内容。",
    ]
)


def _build_review_user_prompt(input: SpawnSubagentInput, summary: str) -> str:
    current_date = datetime.now(timezone.utc).date().isoformat()
    return "\n\n".join(
        [
            f"## 当前日期\n{current_date}",
            f"## 子 Agent 角色\n{input.role_prompt}",
            f"## 任务与验收标准\n{input.task}",
            f"## 待评审产出\n{summary}",
        ]
    )


@dataclass
class ReviewOutcome:
    """评审器故障时的兜底结论：不阻断主流程（fail-open），也不浪费重派额度。"""

    skipped: bool
    verdict: ReviewVerdict | None = None


# 结构化输出按 json_mode → function_calling 顺序尝试：DeepSeek 等 OpenAI 兼容服务
# 不支持 response_format=json_schema，部分模型的 thinking 模式也不支持强制 tool_choice，
# json_mode（response_format=json_object + 提示词约束 JSON）兼容性最好，故优先。
_REVIEW_STRUCTURED_METHODS = ("json_mode", "function_calling")


async def review_subagent_output(
    model: Any,
    input: SpawnSubagentInput,
    summary: str,
    signal: AbortSignal | None = None,
) -> ReviewOutcome:
    """评审一次子 Agent 产出。

    fail-open：模型不可用、全部结构化方式都失败、超时或主 run 取消时返回 skipped，
    调用方按「无评审」放行原摘要——评审器自身的故障不能拖垮整条派发链路。
    """
    if model is None or (signal is not None and signal.aborted):
        return ReviewOutcome(skipped=True)
    last_reason = "unknown"

    async def _review_all() -> ReviewOutcome:
        nonlocal last_reason
        for method in _REVIEW_STRUCTURED_METHODS:
            if signal is not None and signal.aborted:
                break
            try:
                structured = model.with_structured_output(
                    _ReviewVerdictLenient, method=method
                )
                raw = await structured.ainvoke(
                    [
                        SystemMessage(content=REVIEW_SYSTEM_PROMPT),
                        HumanMessage(content=_build_review_user_prompt(input, summary)),
                    ],
                    config={"callbacks": [], "tags": ["nostream"]},
                )
                if isinstance(raw, _ReviewVerdictLenient):
                    parsed = raw
                elif isinstance(raw, dict):
                    parsed = _ReviewVerdictLenient.model_validate(raw)
                else:
                    last_reason = f"verdict parse failed ({method}): unexpected payload"
                    continue
                # 再过一遍长度收敛，保证事件载荷一定满足 contracts 上限。
                verdict = ReviewVerdict(
                    passed=parsed.passed,
                    score=parsed.score,
                    feedback=clamp_subagent_text(parsed.feedback, 1_000),
                    checklist=[
                        _ChecklistItem(item=clamp_subagent_text(item.item, 300), met=item.met)
                        for item in parsed.checklist[:20]
                    ],
                )
                return ReviewOutcome(skipped=False, verdict=verdict)
            except Exception as error:
                last_reason = str(error)
        return ReviewOutcome(skipped=True)

    try:
        outcome = await asyncio.wait_for(_review_all(), timeout=REVIEW_TIMEOUT_MS / 1000)
    except Exception as error:
        last_reason = str(error)
        outcome = ReviewOutcome(skipped=True)
    # 静默 skip 曾导致评审器形同虚设且无任何线索；跳过（非主 run 取消）时留一条 warn。
    if outcome.skipped and not (signal is not None and signal.aborted):
        logger.warning("[subagent] reviewer skipped (fail-open): %s", clamp_subagent_text(last_reason, 300))
    return outcome


def append_review_gap(summary: str, verdict: ReviewVerdict) -> str:
    """未达标且重派额度耗尽时，把评审差距附在摘要后如实交回主 Agent。"""
    missed = [f"- {item.item}" for item in verdict.checklist if not item.met]
    note_parts = [
        "## 评审未通过（已达重派上限，以下差距未闭合）",
        f"质量分：{verdict.score}/100",
    ]
    if missed:
        note_parts.append(f"未满足的验收项：\n{'\n'.join(missed)}")
    if verdict.feedback:
        note_parts.append(f"整改意见：\n{verdict.feedback}")
    note = "\n\n".join(note_parts)
    room = SUBAGENT_SUMMARY_MAX_CHARS - len(note) - 2
    head = clamp_subagent_text(summary, room) if room > 0 else ""
    combined = f"{head}{'\n\n' if head else ''}{note}"
    return clamp_subagent_text(combined, SUBAGENT_SUMMARY_MAX_CHARS)


@dataclass
class SpawnLoopDeps:
    """spawn_subagent 工具工厂的可替换依赖（测试注入用，生产路径用默认实现）。"""

    run: Callable[[SpawnSubagentOptions, SpawnSubagentInput], Awaitable[_SubagentRunOutcome]] = run_subagent
    review: Callable[[Any, SpawnSubagentInput, str, AbortSignal | None], Awaitable[ReviewOutcome]] = review_subagent_output
    emit: Callable[[Any, dict[str, Any]], None] = emit_subagent_event


def describe_subagent_input(input: SpawnSubagentInput) -> tuple[str, str]:
    """从派发输入提取卡片展示用的角色名（首行非空文本）与任务简述。"""
    first_line = next(
        (line.strip() for line in input.role_prompt.split("\n") if line.strip()),
        "子任务",
    )
    return (
        clamp_subagent_text(first_line, ROLE_MAX_CHARS),
        clamp_subagent_text(input.task, DESCRIPTION_MAX_CHARS),
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def run_spawn_loop(
    options: SpawnSubagentOptions,
    input: SpawnSubagentInput,
    config: Any,
    deps: SpawnLoopDeps | None = None,
    override: dict[str, Any] | None = None,
) -> str:
    """评审-重派闭环主体：每轮 派发→完成→评审，不达标带 feedback 重派至上限。

    失败/超时/评审器跳过时直接放行当轮摘要。
    override：后台派发时由调用方预生成 subagent_id（要在立即返回的 ack 里告知主 Agent），
    并在 started 事件上标记 background。
    """
    deps = deps or SpawnLoopDeps()
    override = override or {}
    subagent_id = override.get("subagent_id") or uuid.uuid4().hex
    role, description = describe_subagent_input(input)

    attempt = 1
    prior_feedback: str | None = None
    while True:
        run_input = (
            input.model_copy(update={"prior_feedback": prior_feedback})
            if prior_feedback
            else input
        )
        started_at = datetime.now(timezone.utc)
        started_event: dict[str, Any] = {
            "runId": options.run_id,
            "timestamp": _timestamp(),
            "type": "subagent.started",
            "subagentId": subagent_id,
            "role": role,
            "description": description,
            "attempt": attempt,
        }
        if override.get("background"):
            started_event["background"] = True
        deps.emit(config, started_event)
        result = await deps.run(options, run_input)
        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        deps.emit(
            config,
            {
                "runId": options.run_id,
                "timestamp": _timestamp(),
                "type": "subagent.completed",
                "subagentId": subagent_id,
                "attempt": attempt,
                "status": result.status,
                "summary": result.summary,
                "toolCalls": result.tool_calls,
                "durationMs": duration_ms,
            },
        )

        # 失败/超时不评审：失败摘要本身已告诉主 Agent 如何处置（拆小任务/基于部分产出继续）。
        if result.status != "completed":
            return result.summary
        if options.signal is not None and options.signal.aborted:
            return result.summary

        review = await deps.review(options.router.primary, run_input, result.summary, options.signal)
        if review.skipped or review.verdict is None:
            return result.summary

        deps.emit(
            config,
            {
                "runId": options.run_id,
                "timestamp": _timestamp(),
                "type": "subagent.reviewed",
                "subagentId": subagent_id,
                "attempt": attempt,
                "passed": review.verdict.passed,
                "score": review.verdict.score,
                "feedback": review.verdict.feedback,
                "checklist": [item.model_dump() for item in review.verdict.checklist],
            },
        )

        if review.verdict.passed:
            return result.summary
        if attempt >= SUBAGENT_MAX_ATTEMPTS:
            # 如实汇报：最终产出 + 未闭合的差距，主 Agent 自行决定是否补救。
            return append_review_gap(result.summary, review.verdict)
        attempt += 1
        prior_feedback = review.verdict.feedback or "请对照未满足的验收项补齐缺失内容。"


@dataclass
class BackgroundTaskResult:
    """一个后台子任务 settle 后的结果。"""

    subagent_id: str
    role: str
    description: str
    # 最终摘要（含评审未过上限时的差距说明）；异常中止时为中止说明。
    summary: str


# 后台事件队列在无人读取时的缓冲上限：超过说明消费端异常，丢弃最旧事件防内存膨胀。
BACKGROUND_EVENT_QUEUE_MAX = 500
# abort_all 的收尾宽限：不能让 interrupt/error 路径被卡住。
BACKGROUND_ABORT_GRACE_MS = 3_000


@dataclass
class _BackgroundEntry:
    subagent_id: str
    role: str
    description: str
    controller: AbortController
    done: bool = False
    task: asyncio.Task[BackgroundTaskResult] | None = None


class BackgroundRunContext:
    """单次 execute 内的后台任务登记表（P3）。

    生命周期严格限定在一个 run 内：人审挂起/失败/取消时 abort_all 收割，
    不做跨进程持久化（跨 run 的后台续跑是后续独立能力）。
    """

    def __init__(self, run_signal: AbortSignal | None = None) -> None:
        self._run_signal = run_signal
        self._entries: list[_BackgroundEntry] = []
        self._queue: list[dict[str, Any]] = []

    def _emit(self, event: dict[str, Any]) -> None:
        self._queue.append(event)
        if len(self._queue) > BACKGROUND_EVENT_QUEUE_MAX:
            del self._queue[: len(self._queue) - BACKGROUND_EVENT_QUEUE_MAX]

    def register(
        self,
        *,
        subagent_id: str,
        role: str,
        description: str,
        run: Callable[[Callable[[dict[str, Any]], None], AbortSignal], Awaitable[str]],
    ) -> None:
        controller = AbortController()
        # AbortSignal.any 的等价：主 run 信号中止时联动子任务。
        if self._run_signal is not None:
            self._run_signal.add_event_listener(controller.abort)
        entry = _BackgroundEntry(
            subagent_id=subagent_id,
            role=role,
            description=description,
            controller=controller,
        )

        async def _runner() -> BackgroundTaskResult:
            try:
                summary = await run(self._emit, controller.signal)
                return BackgroundTaskResult(
                    subagent_id=subagent_id,
                    role=role,
                    description=description,
                    summary=summary,
                )
            except Exception as error:
                return BackgroundTaskResult(
                    subagent_id=subagent_id,
                    role=role,
                    description=description,
                    summary=f"后台子 Agent 异常中止：{clamp_subagent_text(str(error), 500)}",
                )
            finally:
                entry.done = True

        entry.task = asyncio.ensure_future(_runner())
        self._entries.append(entry)

    def size(self) -> int:
        """尚未结束的后台任务数。"""
        return sum(1 for entry in self._entries if not entry.done)

    def drain_events(self) -> list[dict[str, Any]]:
        """非阻塞取出已缓冲事件。"""
        drained = self._queue[:]
        self._queue.clear()
        return drained

    async def settled(self) -> list[BackgroundTaskResult]:
        """等待全部后台任务结束并取回摘要（事件应先/再 drain_events 取净）。"""
        tasks = [entry.task for entry in self._entries if entry.task is not None]
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    async def abort_all(self) -> None:
        """中止全部后台任务（人审挂起/run 失败/取消），给 3s 收尾宽限。"""
        for entry in self._entries:
            if not entry.done:
                entry.controller.abort()
        tasks = [entry.task for entry in self._entries if entry.task is not None]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=BACKGROUND_ABORT_GRACE_MS / 1000)
        for task in pending:
            task.cancel()


def create_background_run_context(run_signal: AbortSignal | None = None) -> BackgroundRunContext:
    return BackgroundRunContext(run_signal)


_SPAWN_TOOL_DESCRIPTION = (
    "派发一个隔离子 Agent 执行单项任务，只返回最终摘要（≤2000 字），子 Agent 的中间执行过程不会进入对话。"
    "role_prompt 写清角色职责边界、工作方法与输出要求；task 必须写清目标与可核对的验收标准（评审器据此自动验收）；关键背景/文件路径放 context。"
    "同步模式（默认）：等待完成后继续，内部自动评审、不达标带整改意见重派（最多 3 轮），返回即通过评审；"
    "background=true（异步模式）：工具立即返回 taskId，你先给用户阶段性回复，任务在后台并行执行，完成后系统自动续轮把摘要交回做最终汇总——适合耗时较长、希望先响应用户的任务。"
    "适用：可独立交付的调研、检索、分析、验证类子任务；需要用户确认的事项不要派发。"
    "不适用：简单事实查询（如商品参数查取）或 2-3 项直接对比——主 Agent 用 自带的搜索工具 搜 1-2 次更高效；spawn 的开销只在任务有多源、可并行、需隔离或篇幅明显较长时才值得。"
    "task 里的验收标准应关注信息完整性、来源可靠性和结论准确性，不要写硬性字数上限、格式模板或措辞风格等机械指标。"
)


def create_spawn_subagent_tool(
    options: SpawnSubagentOptions,
    *,
    run_spawn_loop_fn: Callable[..., Awaitable[str]] | None = None,
    run_fn: Callable[..., Awaitable[_SubagentRunOutcome]] | None = None,
    review_fn: Callable[..., Awaitable[ReviewOutcome]] | None = None,
) -> Any:
    """spawn_subagent 工具：主 Agent 的子任务派发入口。

    P2：成功产出先过评审器，不达标带 prior_feedback 重派（最多 SUBAGENT_MAX_ATTEMPTS 轮）。
    P3：background=true 时任务不阻塞——工具立即返回 ack，子 Agent 脱离当前工具调用在
    同一个 run 内继续执行（事件走后台事件队列），execute 收尾时等待全部后台任务，
    再以检查点续一轮把摘要交回主 Agent 汇总；人审挂起/失败/取消则中止全部后台任务。
    """
    loop_fn = run_spawn_loop_fn or run_spawn_loop

    async def _impl(
        role_prompt: str,
        task: str,
        tools_allowlist: list[str] | None = None,
        model_tier: str = "fast",
        context: str | None = None,
        prior_feedback: str | None = None,
        background: bool = False,
        config: Any = None,
    ) -> str:
        input_data = SpawnSubagentInput(
            role_prompt=role_prompt,
            task=task,
            tools_allowlist=tools_allowlist,
            model_tier=model_tier,  # type: ignore[arg-type]
            context=context,
            prior_feedback=prior_feedback,
            background=background,
        )
        if input_data.background:
            configurable = (config or {}).get("configurable", {}) if isinstance(config, dict) else {}
            bg: BackgroundRunContext | None = configurable.get("backgroundCtx") or configurable.get(
                "background_ctx"
            )
            if bg is None:
                return (
                    "后台派发运行上下文不可用。请改用同步模式（background=false）重新调用；"
                    "若问题持续，直接以普通方式完成任务，不要重复尝试后台派发。"
                )
            subagent_id = uuid.uuid4().hex
            role, description = describe_subagent_input(input_data)

            async def _bg_run(
                emit: Callable[[dict[str, Any]], None],
                signal: AbortSignal,
            ) -> str:
                bg_options = SpawnSubagentOptions(
                    run_id=options.run_id,
                    router=options.router,
                    tools=options.tools,
                    backend=options.backend,
                    signal=signal,
                )
                return await loop_fn(
                    bg_options,
                    input_data,
                    None,
                    SpawnLoopDeps(
                        run=run_fn or run_subagent,
                        review=review_fn or review_subagent_output,
                        emit=lambda _config, event: emit(event),
                    ),
                    {"subagent_id": subagent_id, "background": True},
                )

            bg.register(
                subagent_id=subagent_id,
                role=role,
                description=description,
                run=_bg_run,
            )
            return "\n".join(
                [
                    f"后台子任务已启动（taskId={subagent_id}，角色：{role}），不阻塞当前对话。",
                    "请立即给用户一句简短的阶段性说明（已在后台处理、完成后会自动汇总结果），不要空等也不要重复派发；",
                    "任务结束后系统会自动把评审通过的摘要交回，届时你再基于结果做最终汇总。",
                ]
            )
        return await loop_fn(options, input_data, config)

    return tool(
        "spawn_subagent",
        args_schema=SpawnSubagentInput,
        description=_SPAWN_TOOL_DESCRIPTION,
        coroutine=_impl,
    )
