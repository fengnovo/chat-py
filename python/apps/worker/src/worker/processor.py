"""运行任务处理器 — 镜像 apps/worker/src/processor.ts。

消费 RunJob，执行 Agent 运行并持久化事件到 DB，发布到 Redis channel。
arq 通过上下文注入服务，本模块提供 create_run_processor 返回任务函数。
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
import uuid
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import TypeAdapter

from contracts import (
    AgentEvent,
    RunCancelledEvent,
    RunFailedEvent,
    RunJob,
    run_events_channel,
)
from db import AgentRepository, MemoryJobInput

from .agent_telemetry import AgentTelemetry
from .config import WorkerConfig
from .langfuse import WorkerLangfuse
from .lock import with_session_lock
from .observability import WorkerObservability
from .redis_circuit_breaker import RedisCircuitBreakerStore
from .workspace import (
    AgentResources,
    RemoteWorkspaceSandbox,
    prepare_workspace,
    remote_workspace_path,
    safe_relative_path,
    upload_agent_resources,
)


_run_job_adapter: TypeAdapter[RunJob] = TypeAdapter(RunJob)
_agent_event_adapter: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


def metric_job_kind(kind: str) -> str:
    return "run" if kind == "start" else "resume"


logger = logging.getLogger("worker.processor")


def resolve_worker_mcp_config_path(
    configured_path: str | None,
) -> str | None:
    """Worker MCP 配置路径透传。默认路径已在 config.ts 按 NODE_ENV 选定。"""
    return configured_path


def _safely(action: Any) -> Any:
    try:
        return action()
    except Exception:
        return None


def _terminal_status(event: AgentEvent) -> str | None:
    if event.type == "run.completed":
        return "completed"
    if event.type == "run.cancelled":
        return "cancelled"
    if event.type == "run.failed":
        return "failed"
    if event.type == "approval.required":
        return "waiting_approval"
    if event.type == "question.required":
        return "waiting_question"
    return None


async def persist_event(
    services: dict[str, Any],
    job: RunJob,
    event: AgentEvent,
) -> None:
    repository: AgentRepository = services["repository"]
    publisher = services["publisher"]
    memory_queue = services["memory_queue"]

    validated = event
    persisted = await repository.append_event(str(job.tenant_id), validated)
    await publisher.publish(
        run_events_channel(str(job.run_id)), str(persisted["seq"])
    )
    if event.type == "approval.required":
        await repository.create_interrupt(str(job.tenant_id), str(job.run_id), {
            "id": event.interrupt_id,
            "kind": "approval",
            "request": event.actions,
        })
    elif event.type == "question.required":
        await repository.create_interrupt(str(job.tenant_id), str(job.run_id), {
            "id": event.interrupt_id,
            "kind": "question",
            "request": event.question,
        })
    status = _terminal_status(event)
    if status:
        error = None
        if event.type == "run.failed":
            error = {"code": event.code, "message": event.message}
        await repository.update_run_status(
            str(job.tenant_id), str(job.run_id), status, error
        )
        if status == "completed":
            await repository.enqueue_memory_job(MemoryJobInput(
                tenant_id=str(job.tenant_id),
                user_id=str(job.user_id),
                session_id=str(job.session_id),
                run_id=str(job.run_id),
            ))
            try:
                await memory_queue.enqueue_job(
                    "extract",
                    {"runId": str(job.run_id)},
                    _job_id=f"memory-{job.run_id}",
                )
            except Exception:
                pass


# 进程级沙箱缓存：按 workspaceId 复用活跃连接，跳过重复的 E2B connect/create 网络往返。
_sandbox_cache: dict[str, dict[str, Any]] = {}


def _sandbox_cache_ttl_ms(config_timeout_ms: int | None) -> float:
    return (config_timeout_ms or 600_000) * 0.8 / 1000.0


def _get_cached_sandbox(workspace_id: str, timeout_ms: int) -> Any | None:
    entry = _sandbox_cache.get(workspace_id)
    if not entry:
        return None
    age = time.time() - entry["last_used_at"]
    if age > _sandbox_cache_ttl_ms(entry["timeout_ms"]):
        _sandbox_cache.pop(workspace_id, None)
        sandbox = entry["sandbox"]
        if hasattr(sandbox, "pause"):
            asyncio.create_task(sandbox.pause())
        return None
    return entry["sandbox"]


def _put_cached_sandbox(workspace_id: str, sandbox: Any, timeout_ms: int) -> None:
    _sandbox_cache[workspace_id] = {
        "sandbox": sandbox,
        "last_used_at": time.time(),
        "timeout_ms": timeout_ms,
    }


def _evict_cached_sandbox(workspace_id: str) -> Any | None:
    return _sandbox_cache.pop(workspace_id, None)


async def acquire_sandbox(
    services: dict[str, Any],
    job: RunJob,
    workspace: dict[str, Any],
    signal: AbortSignal,
) -> Any:
    config: WorkerConfig = services["config"]
    cached = _get_cached_sandbox(workspace["workspace_id"], config.E2B_TIMEOUT_MS or 600_000)
    if cached:
        if hasattr(cached, "set_timeout") and config.E2B_TIMEOUT_MS:
            asyncio.create_task(cached.set_timeout(config.E2B_TIMEOUT_MS))
        return cached

    sandbox = None
    if config.SANDBOX_RUNTIME == "docker":
        try:
            from agent_core import DockerSandboxBackend, DockerSandboxOptions

            options = DockerSandboxOptions(
                session_id=workspace["workspace_id"],
                root_directory=config.DOCKER_SANDBOX_SESSIONS_ROOT,
                image=config.DOCKER_SANDBOX_IMAGE,
                command_timeout_ms=config.DOCKER_SANDBOX_COMMAND_TIMEOUT_MS,
            )
            sandbox = await DockerSandboxBackend.create(options)
        except Exception:
            logger.exception("docker sandbox acquire failed")
            sandbox = None
    else:
        api_key = config.E2B_API_KEY
        if not api_key:
            raise RuntimeError("E2B_API_KEY is required for e2b-cloud")
        try:
            from agent_core import E2BSandbox, E2BSandboxOptions

            sandbox_options = E2BSandboxOptions(
                api_key=api_key,
                **({"api_url": config.E2B_API_URL} if config.E2B_API_URL else {}),
                **({"sandbox_url": config.E2B_SANDBOX_URL} if config.E2B_SANDBOX_URL else {}),
                template=config.E2B_TEMPLATE,
                timeout_ms=config.E2B_TIMEOUT_MS,
                signal=signal,
            )
            if workspace.get("sandbox_id"):
                try:
                    sandbox = await E2BSandbox.connect(workspace["sandbox_id"], sandbox_options)
                except Exception:
                    logger.warning("stale e2b sandbox id, creating new one", exc_info=True)
                    await services["repository"].clear_workspace_sandbox_id(
                        str(job.tenant_id),
                        workspace["workspace_id"],
                        workspace["sandbox_id"],
                    )
                    sandbox = await E2BSandbox.create(sandbox_options)
                    saved = await services["repository"].save_workspace_sandbox_id(
                        str(job.tenant_id),
                        workspace["workspace_id"],
                        sandbox.id,
                    )
                    if not saved:
                        await sandbox.kill()
                        raise RuntimeError("Workspace sandbox identity changed concurrently")
            else:
                sandbox = await E2BSandbox.create(sandbox_options)
                saved = await services["repository"].save_workspace_sandbox_id(
                    str(job.tenant_id),
                    workspace["workspace_id"],
                    sandbox.id,
                )
                if not saved:
                    await sandbox.kill()
                    raise RuntimeError("Workspace sandbox identity changed concurrently")
        except Exception:
            logger.exception("e2b sandbox acquire failed")
            sandbox = None

    if sandbox is None:
        raise RuntimeError("Failed to acquire sandbox backend")
    _put_cached_sandbox(
        workspace["workspace_id"], sandbox, config.E2B_TIMEOUT_MS or 600_000
    )
    return sandbox


async def build_long_term_memory(
    services: dict[str, Any],
    job: RunJob,
    project_id: str | None,
    query: str,
    options: dict[str, Any] | None = None,
) -> Any | None:
    """读取长期记忆并写入 DeepAgents Store 的 /memories/profile.md。

    任何子步骤失败都 fail-open：记录指标后返回 None，让对话正常进行。
    """
    retrieve_started_at = time.monotonic()
    try:
        assistant_key = "chat"
        scope = f"project:{project_id}" if project_id else "global"
        repository: AgentRepository = services["repository"]
        memory_index = services.get("memory_index")
        memory_store = services.get("memory_store")

        # 检索记忆
        records = []
        if memory_index and hasattr(memory_index, "search"):
            records = await memory_index.search(
                query, str(job.tenant_id), str(job.user_id), 12, project_id
            )

        if metrics := services.get("memory_metrics"):
            if hasattr(metrics, "memory_operation"):
                metrics.memory_operation(
                    operation="retrieve",
                    outcome="success",
                    duration_ms=(time.monotonic() - retrieve_started_at) * 1000,
                )

        if records and memory_store:
            # 待 agent-core 提供 writeLongTermMemoryProfile 后写入 store
            pass

        # 把语义召回结果渲染成 context；deep_agent 会以 <long_term_memory>
        # 注入系统提示（store/remember 工具可选，缺失也不影响只读召回）。
        context_lines: list[str] = []
        for record in records:
            content = (
                record.get("content", "")
                if isinstance(record, dict)
                else getattr(record, "content", "")
            )
            if not content:
                continue
            kind = (
                record.get("kind", "")
                if isinstance(record, dict)
                else getattr(record, "kind", "")
            )
            context_lines.append(f"- [{kind}] {content}" if kind else f"- {content}")
        context = "\n".join(context_lines)

        return {
            "store": memory_store,
            "namespace": {"tenantId": str(job.tenant_id), "userId": str(job.user_id), "assistantKey": assistant_key, "scope": scope},
            "records": records,
            "context": context,
        }
    except Exception:
        if metrics := services.get("memory_metrics"):
            if hasattr(metrics, "memory_operation"):
                metrics.memory_operation(
                    operation="retrieve",
                    outcome="failure",
                    duration_ms=(time.monotonic() - retrieve_started_at) * 1000,
                )
        return None


async def create_runtime(
    services: dict[str, Any],
    job: RunJob,
    workspace_path: str,
    backend: Any,
    signal: AbortSignal,
    agent_telemetry: AgentTelemetry | None = None,
    callbacks: list[Any] | None = None,
    agent_resources: AgentResources | None = None,
    long_term_memory: Any | None = None,
) -> Any:
    """创建 DeepAgent runtime（待 agent-core 迁移后替换）。"""
    config: WorkerConfig = services["config"]
    knowledge_mcp_enabled = (
        config.KNOWLEDGE_MCP_ENABLED == "true"
        and config.KNOWLEDGE_MCP_URL
        and config.KNOWLEDGE_MCP_SECRET
        and len(job.knowledge_base_ids) > 0
    )
    knowledge_token = (
        await create_knowledge_run_token(job, config.KNOWLEDGE_MCP_SECRET)
        if knowledge_mcp_enabled
        else ""
    )
    mcp_config_path = resolve_worker_mcp_config_path(config.MCP_CONFIG_PATH)

    try:
        from agent_core import (
            HeadlessAgentOptions,
            KnowledgeMcpOptions,
            LongTermMemoryOptions,
            ModelSpec,
            SummarizationConfig,
            create_deep_agent_runtime,
        )

        ltm_options = None
        # store 可选：没有 store/remember 回调时，仍可通过 context 做只读记忆注入。
        if long_term_memory and (
            long_term_memory.get("store") or long_term_memory.get("context")
        ):
            ltm_options = LongTermMemoryOptions(
                store=long_term_memory.get("store"),
                namespace=[
                    "memory",
                    str(job.tenant_id),
                    str(job.user_id),
                ],
                context=long_term_memory.get("context") or None,
            )

        options = HeadlessAgentOptions(
            run_id=str(job.run_id),
            session_id=str(job.session_id),
            workspace_path=workspace_path,
            backend=backend,
            backend_mode="docker" if config.SANDBOX_RUNTIME == "docker" else "e2b",
            checkpointer=services.get("checkpointer"),
            models=[
                ModelSpec(
                    id=m.id,
                    model=m.model,
                    provider=m.provider,
                    apiKey=m.api_key,
                    baseUrl=m.base_url,
                    maxTokens=m.max_tokens,
                )
                for m in config.models
            ],
            circuit_breaker=RedisCircuitBreakerStore(
                services["redis"],
                str(job.tenant_id),
                telemetry=agent_telemetry,
            ),
            telemetry=agent_telemetry,
            callbacks=callbacks or [],
            auto_approve_tools=job.approval_mode == "session",
            recursion_limit=config.AGENT_RECURSION_LIMIT,
            model_call_limit=config.AGENT_MODEL_CALL_LIMIT,
            signal=signal,
            mcp_config_path=mcp_config_path,
            knowledge_mcp=KnowledgeMcpOptions(
                url=config.KNOWLEDGE_MCP_URL or "",
                token=knowledge_token,
                timeout_ms=config.KNOWLEDGE_MCP_TIMEOUT_MS,
                enabled=knowledge_mcp_enabled,
            ),
            memory=agent_resources.memory if agent_resources and agent_resources.memory else None,
            skills=agent_resources.skills if agent_resources and agent_resources.skills else None,
            long_term_memory=ltm_options,
            summarization=SummarizationConfig(
                trigger_tokens=config.AGENT_SUMMARIZATION_TRIGGER_TOKENS,
                keep_tokens=config.AGENT_SUMMARIZATION_KEEP_TOKENS,
                truncate_args_tokens=40_000,
            ),
        )
        runtime = await create_deep_agent_runtime(options)
        return runtime
    except Exception:
        logger.exception("agent runtime creation failed")
        raise


async def create_knowledge_run_token(job: RunJob, secret: str) -> str:
    """创建 knowledge-service 的短期 JWT。"""
    from jose import jwt
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    claims = {
        "tenantId": str(job.tenant_id),
        "userId": str(job.user_id),
        "sessionId": str(job.session_id),
        "runId": str(job.run_id),
        "kbIds": [str(k) for k in job.knowledge_base_ids],
        "jti": str(uuid.uuid4()),
        "aud": "knowledge-service",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


_IMAGE_ATTACHMENT_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


async def prepare_run_attachments(
    services: dict[str, Any],
    sandbox: RemoteWorkspaceSandbox,
    remote_path: str,
    attachments: list[Any],
) -> dict[str, Any]:
    """run 开始前按引用取回用户附件，按类型分流。"""
    images: list[dict[str, Any]] = []
    appended_message = ""
    sandbox_files: list[tuple[str, bytes]] = []
    uploaded_names: list[str] = []

    for attachment in attachments:
        stored_bytes = await services["artifacts"].get_object_bytes(attachment.object_key)
        bytes_data = (
            gzip.decompress(stored_bytes)
            if attachment.content_encoding == "gzip"
            else stored_bytes
        )
        if attachment.kind == "image":
            if attachment.content_type not in _IMAGE_ATTACHMENT_MEDIA_TYPES:
                raise RuntimeError(
                    f"Unsupported image attachment type: {attachment.content_type}"
                )
            images.append({
                "media_type": attachment.content_type,
                "data_url": f"data:{attachment.content_type};base64,{__import__('base64').b64encode(bytes_data).decode()}",
                **({"filename": attachment.filename} if attachment.filename else {}),
            })
        elif attachment.kind == "text":
            appended_message += f"\n\n[附件 {attachment.filename}]\n{bytes_data.decode('utf-8')}"
        else:
            relative_path = safe_relative_path(attachment.filename)
            sandbox_files.append(
                (f"{remote_path}/{relative_path}", bytes_data)
            )
            uploaded_names.append(relative_path)

    if sandbox_files:
        def _quote(v: str) -> str:
            return "'" + v.replace("'", "'\\''") + "'"
        directories = list({str(p.rsplit("/", 1)[0]) for p, _ in sandbox_files})
        await sandbox.execute(
            f"mkdir -p {' '.join(_quote(d) for d in directories)}"
        )
        results = await sandbox.upload_files(sandbox_files)
        for r in results:
            if r.get("error"):
                raise RuntimeError(
                    f"Failed to upload attachment {r.get('path')}: {r.get('error')}"
                )
        appended_message += (
            "\n\n以下附件已上传到工作区根目录："
            + "、".join(f"`{n}`" for n in uploaded_names)
            + "。请直接在工作区中读取使用。"
        )

    return {"appended_message": appended_message, "images": images}


async def kill_persisted_sandbox(
    services: dict[str, Any], job: RunJob
) -> None:
    """取消或失败时清理沙箱。docker 模式无需处理。"""
    config: WorkerConfig = services["config"]
    if config.SANDBOX_RUNTIME == "docker":
        return

    repository: AgentRepository = services["repository"]
    workspace = await repository.get_workspace_sandbox_for_worker(
        str(job.tenant_id), str(job.session_id)
    )
    api_key = config.E2B_API_KEY
    if not workspace or not workspace.sandbox_id or not api_key:
        return
    try:
        from agent_core import E2BSandbox  # type: ignore[import-not-found]
        sandbox = await E2BSandbox.connect(workspace.sandbox_id, {
            "api_key": api_key,
            **({"api_url": config.E2B_API_URL} if config.E2B_API_URL else {}),
            **({"sandbox_url": config.E2B_SANDBOX_URL} if config.E2B_SANDBOX_URL else {}),
            "template": config.E2B_TEMPLATE,
            "timeout_ms": config.E2B_TIMEOUT_MS,
        })
        await sandbox.kill()
    except Exception:
        pass
    await repository.clear_workspace_sandbox_id(
        str(job.tenant_id), workspace.workspace_id, workspace.sandbox_id
    )


def create_run_processor(
    services: dict[str, Any],
    telemetry: WorkerObservability | None = None,
    langfuse: WorkerLangfuse | None = None,
) -> Any:
    """返回 arq 任务函数。"""

    async def _run_processor(ctx: dict[str, Any], *args: Any, **job_kwargs: Any) -> None:
        # arq 把 enqueue_job 的 dict 作为位置参数传入，同时兼容 kwargs 形式
        payload: dict[str, Any] = {}
        if args and isinstance(args[0], dict):
            payload.update(args[0])
        payload.update(job_kwargs)
        # 从 arq job data 解析 RunJob（union 类型需用 TypeAdapter）
        job: RunJob = _run_job_adapter.validate_python(payload)

        repository: AgentRepository = services["repository"]
        redis = services["redis"]
        publisher = services["publisher"]
        controllers: dict[str, AbortController] = services["controllers"]

        producer_context = job.observability or {}
        # arq 将 job_id / enqueue_time 注入 ctx 而非 kwargs
        job_id = ctx.get("job_id") or job_kwargs.get("_job_id")
        enqueue_time = ctx.get("enqueue_time")
        wait_ms = job_kwargs.get("_enqueue_time_ms")
        if wait_ms is None and enqueue_time is not None:
            from datetime import datetime, timezone
            wait_ms = (datetime.now(timezone.utc) - enqueue_time).total_seconds() * 1000
        started_at = time.monotonic()
        span = None
        if telemetry:
            span = telemetry.runtime.tracer.start_span(
                "worker.job.execute",
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "arq",
                    "messaging.destination.name": "agent-runs",
                    "messaging.operation.name": "process",
                    "messaging.message.id": str(job_id or ""),
                    "job.kind": job.kind,
                    "run_id": str(job.run_id),
                    **({"queue.wait.ms": wait_ms} if wait_ms is not None else {}),
                },
            )
        job_kind = metric_job_kind(job.kind)
        if wait_ms is not None and telemetry:
            _safely(lambda: telemetry.metrics.queue_wait(
                queue="agent-runs", job=job_kind, wait_ms=wait_ms
            ))

        job_outcome = "completed"
        agent_telemetry = AgentTelemetry(telemetry) if telemetry else None

        async def observed(operation: str, action: Any) -> Any:
            if agent_telemetry:
                return await agent_telemetry.run_span(
                    {"run_id": str(job.run_id), "operation": operation}, action
                )
            return await action()

        try:
            if span:
                from opentelemetry import context as otel_context
                ctx_token = otel_context.attach(trace.set_span_in_context(span))
            else:
                ctx_token = None
            try:
                if job_id:
                    await repository.mark_dispatch_consumed(str(job_id))
                await with_session_lock(
                    redis,
                    str(job.session_id),
                    lambda: _process_run_inner(
                        services, job, telemetry, langfuse, agent_telemetry,
                        repository, controllers, observed, span,
                    ),
                    lambda outcome, duration_ms: _safely(
                        lambda: agent_telemetry.phase({
                            "operation": "session.lock.acquire",
                            "outcome": outcome,
                            "duration_ms": duration_ms,
                        }) if agent_telemetry else None
                    ),
                )
            finally:
                if ctx_token:
                    from opentelemetry import context as otel_context
                    otel_context.detach(ctx_token)
        except Exception:
            job_outcome = "failed"
            _safely(lambda: telemetry.metrics.queue_job(
                queue="agent-runs", job=job_kind, outcome="failed",
                duration_ms=(time.monotonic() - started_at) * 1000,
            ) if telemetry else None)
            if span:
                span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            _safely(lambda: (
                telemetry.metrics.queue_job(
                    queue="agent-runs", job=job_kind, outcome="completed",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                ) if job_outcome == "completed" and telemetry else None,
                span.end() if span else None,
            ))

    return _run_processor


async def _process_run_inner(
    services: dict[str, Any],
    job: RunJob,
    telemetry: WorkerObservability | None,
    langfuse: WorkerLangfuse | None,
    agent_telemetry: AgentTelemetry | None,
    repository: AgentRepository,
    controllers: dict[str, AbortController],
    observed: Any,
    span: Any,
) -> None:
    record = await repository.get_run_for_worker(str(job.tenant_id), str(job.run_id))
    if not record:
        raise RuntimeError("Run no longer exists")
    if record.status in ("completed", "failed", "cancelled"):
        return
    if record.cancel_requested_at:
        cancelled = RunCancelledEvent(
            run_id=job.run_id,
            timestamp=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        )
        await persist_event(services, job, cancelled)
        await kill_persisted_sandbox(services, job)
        return

    claimed = await repository.try_mark_run_running(str(job.tenant_id), str(job.run_id))
    if not claimed:
        return
    from agent_core import AbortController

    controller = AbortController()
    controllers[str(job.run_id)] = controller
    runtime = None
    sandbox = None
    workspace_id: str | None = None
    terminal_event_written = False
    should_kill = False
    try:
        workspace = await repository.get_workspace_sandbox_for_worker(
            str(job.tenant_id), str(job.session_id)
        )
        if not workspace:
            raise RuntimeError("Workspace no longer exists")
        workspace_id = workspace.workspace_id
        acquired_sandbox = await observed("sandbox.acquire", lambda: acquire_sandbox(
            services, job, {
                "workspace_id": workspace.workspace_id,
                "sandbox_id": workspace.sandbox_id,
            }, controller.signal,
        ))
        sandbox = acquired_sandbox
        config: WorkerConfig = services["config"]
        remote_path = (
            remote_workspace_path(config.DOCKER_SANDBOX_WORKSPACE_PATH)
            if config.SANDBOX_RUNTIME == "docker"
            else remote_workspace_path(config.E2B_WORKSPACE_PATH)
        )
        session = await repository.get_session_for_worker(str(job.tenant_id), str(job.session_id))
        project_id = session.project_id if session else None

        # 三个独立准备步骤并行执行
        prepared_attachments = {"appended_message": "", "images": []}
        agent_resources = None
        long_term_memory = None

        if job.kind == "start":
            _, prepared_attachments, agent_resources, long_term_memory = await observed(
                "preparation.parallel",
                lambda: asyncio.gather(
                    observed("workspace.prepare", lambda: prepare_workspace(
                        acquired_sandbox, remote_path,
                        job.workspace_source if job.kind == "start" else None,
                        lambda ok: services["artifacts"].get_object_bytes(ok),
                        job.kind == "start",
                    )),
                    observed("attachments.prepare", lambda: prepare_run_attachments(
                        services, acquired_sandbox, remote_path,
                        job.attachments if job.kind == "start" else [],
                    )),
                    observed("agent.resources.upload", lambda: upload_agent_resources(
                        acquired_sandbox, remote_path,
                        config.AGENT_MEMORY_FILE, config.AGENT_SKILLS_DIR,
                    )),
                    observed("memory.retrieve", lambda: build_long_term_memory(
                        services, job, project_id, job.message, {"refresh_profile": True},
                    )),
                ),
            )
        else:
            long_term_memory = await observed("memory.reattach", lambda: build_long_term_memory(
                services, job, project_id, "", {"refresh_profile": False},
            ))

        # Langfuse 按 run 采样
        langchain_callbacks = []
        if langfuse:
            _safely(lambda: langchain_callbacks.extend(langfuse.run_callbacks({
                "run_id": str(job.run_id),
                "session_id": str(job.session_id),
                "user_id": str(job.user_id),
                "run_kind": job.kind,
            })))

        runtime = await observed("agent.runtime.create", lambda: create_runtime(
            services, job, remote_path, acquired_sandbox, controller.signal,
            agent_telemetry, langchain_callbacks, agent_resources, long_term_memory,
        ))

        if telemetry:
            _safely(lambda: telemetry.logger.info(
                "run processing started",
                run_id=str(job.run_id),
                operation="worker.job.execute",
                reason=runtime.mcp_status if runtime else "unknown",
            ))
        else:
            print(f"[processor] run={job.run_id} kbIds={job.knowledge_base_ids} mcp={runtime.mcp_status if runtime else 'unknown'}")

        terminal_event_written, should_kill = await observed(
            "agent.execute",
            lambda: _execute_agent_events(
                services, job, runtime, prepared_attachments, controller
            ),
        )
    except Exception as error:
        should_kill = controller.signal.aborted or not terminal_event_written
        if not terminal_event_written:
            latest = await repository.get_run_for_worker(str(job.tenant_id), str(job.run_id))
            cancelled = controller.signal.aborted or (latest and latest.cancel_requested_at)
            if cancelled:
                event = RunCancelledEvent(
                    run_id=job.run_id,
                    timestamp=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                )
            else:
                event = RunFailedEvent(
                    run_id=job.run_id,
                    timestamp=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                    code="WORKER_EXECUTION_FAILED",
                    message=str(error) if isinstance(error, Exception) else repr(error),
                )
            await persist_event(services, job, event)
        if not controller.signal.aborted:
            raise
    finally:
        controllers.pop(str(job.run_id), None)
        if runtime and hasattr(runtime, "dispose"):
            asyncio.create_task(runtime.dispose())
        await observed("cleanup", lambda: _cleanup_sandbox(
            services, sandbox, workspace_id, should_kill, job,
        ))


async def _execute_agent_events(
    services: dict[str, Any],
    job: RunJob,
    runtime: Any,
    prepared_attachments: dict[str, Any],
    controller: AbortController,
) -> tuple[bool, bool]:
    # 终态标志必须返回给调用方（Python bool 按值传递，形参修改带不出去，
    # 否则完成后清理阶段会误判"没写终态"而补写一条 run.failed）。
    terminal_event_written = False
    should_kill = False
    if job.kind == "start":
        events = runtime.run(
            job.message + prepared_attachments["appended_message"],
            prepared_attachments["images"],
        )
    elif job.kind == "resume-approval":
        events = runtime.resume({
            "kind": "approval",
            "decision": job.decision.decision,
            **({"message": job.decision.message} if job.decision.message else {}),
        })
    else:
        events = runtime.resume({
            "kind": "question",
            "answer": {
                "selections": job.answer.selections,
                **({"customText": job.answer.custom_text} if job.answer.custom_text else {}),
            },
        })

    async for raw_event in events:
        event = (
            raw_event
            if isinstance(raw_event, AgentEvent)
            else _agent_event_adapter.validate_python(raw_event)
        )
        persist_outcome = "success"
        persist_started_at = time.monotonic()
        try:
            await persist_event(services, job, event)
        except Exception as persist_error:
            persist_outcome = "failure"
            raise persist_error
        finally:
            _safely(lambda: None)  # agent_telemetry phase call placeholder
        terminal_event_written = _terminal_status(event) is not None
        if event.type == "run.cancelled":
            should_kill = True
        if event.type == "run.failed" and event.code != "AGENT_STEP_LIMIT":
            should_kill = True

    return terminal_event_written, should_kill


async def _cleanup_sandbox(
    services: dict[str, Any],
    sandbox: Any,
    workspace_id: str | None,
    should_kill: bool,
    job: RunJob,
) -> None:
    repository: AgentRepository = services["repository"]
    if sandbox:
        if should_kill:
            if workspace_id:
                _evict_cached_sandbox(workspace_id)
            if hasattr(sandbox, "kill"):
                await sandbox.kill()
            if workspace_id:
                await repository.clear_workspace_sandbox_id(
                    str(job.tenant_id), workspace_id, sandbox.id if hasattr(sandbox, "id") else ""
                )
            elif hasattr(sandbox, "close"):
                await sandbox.close()
        elif workspace_id:
            config: WorkerConfig = services["config"]
            _put_cached_sandbox(workspace_id, sandbox, config.E2B_TIMEOUT_MS or 600_000)
        else:
            if hasattr(sandbox, "pause"):
                await sandbox.pause()
