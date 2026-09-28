"""Worker 入口 — 镜像 apps/worker/src/worker.ts。

基于 arq 的异步任务队列消费者，替代 BullMQ Worker。
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis_async
from arq import cron
from arq.connections import RedisSettings, create_pool
from arq.worker import Worker, func

from agent_core import AbortController
from db import create_database, migrate_database
from observability import start_observability

from .config import load_worker_config
from .langfuse import create_worker_langfuse
from .memory_consumer import _arq_memory_job, create_memory_queue_processor
from .memory_index import MemoryIndexConfig, create_memory_indexer
from .observability import create_worker_observability
from .processor import create_run_processor

# 全局取消控制器：run_id -> AbortController（agent_core 统一的取消令牌）
_controllers: dict[str, AbortController] = {}

# 全局服务容器
_services: dict[str, Any] = {}


async def _cancellation_listener(publisher: Any) -> None:
    """订阅 agent:run:*:cancel 并 abort 对应 run。"""
    pubsub = publisher.pubsub()
    await pubsub.psubscribe("agent:run:*:cancel")
    try:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                channel = message["channel"]
                run_id = channel.replace("agent:run:", "").replace(":cancel", "")
                if run_id in _controllers:
                    _controllers[run_id].abort()
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.close()


async def _run_job(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> None:
    """arq 任务：执行 Agent run（start / resume-approval / resume-question 共用）。

    实际处理器在 on_startup 中由 create_run_processor 创建并存入 ctx。
    """
    processor = ctx.get("run_processor")
    if processor is None:
        raise RuntimeError("run processor not initialized in worker context")
    await processor(ctx, *args, **kwargs)


async def _memory_index_upsert(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> None:
    """arq 任务：写入记忆索引。"""
    indexer = ctx.get("memory_indexer")
    if not indexer:
        return
    memory: dict[str, Any] = dict(kwargs)
    if args and isinstance(args[0], dict):
        memory = args[0]
    payload = memory.get("memory", memory)
    await indexer.upsert(
        id=str(payload.get("id")),
        tenant_id=str(payload.get("tenantId") or payload.get("tenant_id")),
        user_id=str(payload.get("userId") or payload.get("user_id")),
        content=str(payload.get("content", "")),
        normalized_key=str(payload.get("normalizedKey") or payload.get("normalized_key", "")),
        kind=payload.get("kind"),
        importance=payload.get("importance"),
        confidence=payload.get("confidence"),
        project_id=payload.get("projectId") or payload.get("project_id"),
        scope=payload.get("scope"),
    )


async def _memory_index_delete(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> None:
    """arq 任务：删除记忆索引。"""
    indexer = ctx.get("memory_indexer")
    if not indexer:
        return
    payload: dict[str, Any] = dict(kwargs)
    if args and isinstance(args[0], dict):
        payload = args[0]
    memory_id = payload.get("memory_id") or payload.get("memoryId")
    if memory_id:
        await indexer.remove(str(memory_id))


async def _orphan_attachment_cleanup(ctx: dict[str, Any]) -> None:
    """清理超过 24h 未关联 run 的孤儿聊天附件。"""
    repository = ctx.get("repository")
    artifacts = ctx.get("artifacts")
    logger = ctx.get("logger")
    if not repository or not artifacts:
        return
    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=24)
    stale = await repository.list_stale_unlinked_attachments(cutoff, 100)
    for item in stale:
        remaining = await repository.count_chat_attachments_by_object_key(
            item.object_key, item.id
        )
        if remaining == 0:
            try:
                await artifacts.delete_object(item.object_key)
            except Exception:
                pass
        try:
            await repository.delete_chat_attachment(item.id)
        except Exception:
            pass
    if stale and logger:
        logger.info(f"removed {len(stale)} orphan chat attachments")


def _parse_redis_url(url: str) -> RedisSettings:
    """从 redis://host:port 解析出 RedisSettings。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/")) if parsed.path and parsed.path.lstrip("/") else 0,
    )


class WorkerSettings:
    """arq Worker 配置类（用于 arq 命令行启动）。"""

    # job 名与 API 侧 outbox.enqueue_job(job.kind, ...) 一致；
    # 记忆提取 job 名与 processor 内 memory_queue.enqueue_job("extract", ...) 一致。
    functions = [
        func(_run_job, name="start"),
        func(_run_job, name="resume-approval"),
        func(_run_job, name="resume-question"),
        func(_arq_memory_job, name="extract"),
        func(_memory_index_upsert, name="upsert"),
        func(_memory_index_delete, name="delete"),
        _orphan_attachment_cleanup,
    ]

    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        """arq Worker 启动钩子：初始化所有共享资源。"""
        config = load_worker_config()
        ctx["config"] = config

        # 观测
        otel_config = {
            "OTEL_ENABLED": config.OTEL_ENABLED or "",
            "OTEL_EXPORTER_OTLP_ENDPOINT": config.OTEL_EXPORTER_OTLP_ENDPOINT or "",
        }
        runtime = await start_observability(
            __import__("observability").load_observability_config(
                otel_config,
                service_name="agent-worker",
                service_version="0.1.0",
            )
        )
        observability = create_worker_observability(
            runtime, service_version="0.1.0",
            shutdown_timeout_ms=config.OBSERVABILITY_SHUTDOWN_TIMEOUT_MS,
        )
        ctx["observability"] = observability
        ctx["logger"] = observability.logger

        # Langfuse
        langfuse = create_worker_langfuse(
            shutdown_timeout_ms=config.OBSERVABILITY_SHUTDOWN_TIMEOUT_MS,
            env=dict(os.environ),
        )
        ctx["langfuse"] = langfuse

        # MCP 配置检查
        mcp_path = config.mcp_config_path
        if mcp_path and not os.path.exists(mcp_path):
            raise RuntimeError(
                f"MCP_CONFIG_PATH points to a missing file: {mcp_path}. "
                "Set a repo-relative path (e.g. infra/mcp/mcp.json) or an absolute path that exists."
            )

        # 数据库
        database = await create_database(config.database_url)
        await migrate_database(database.pool)
        ctx["repository"] = database.repository

        # LangGraph checkpointer（Postgres 持久化：审批/追问恢复、worker 重启后续跑）
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer_cm = AsyncPostgresSaver.from_conn_string(config.database_url)
        checkpointer = await checkpointer_cm.__aenter__()
        await checkpointer.setup()
        ctx["checkpointer"] = checkpointer
        ctx["checkpointer_cm"] = checkpointer_cm

        # 孤儿 run 清理
        orphan_runs = await database.repository.fail_orphan_runs_before(
            datetime.now(timezone.utc)
        )
        if orphan_runs > 0:
            observability.logger.warn(f"marked {orphan_runs} orphan run(s) as failed after restart")

        # Redis
        redis_conn = redis_async.from_url(config.REDIS_URL)
        ctx["redis"] = redis_conn
        publisher = redis_async.from_url(config.REDIS_URL)
        ctx["publisher"] = publisher
        cancellation_subscriber = redis_async.from_url(config.REDIS_URL)
        ctx["cancellation_subscriber"] = cancellation_subscriber

        # 取消监听任务
        ctx["cancellation_task"] = asyncio.create_task(
            _cancellation_listener(cancellation_subscriber)
        )

        # S3
        from artifacts import ArtifactStoreConfig, S3ArtifactStore
        artifacts = S3ArtifactStore(ArtifactStoreConfig(
            endpoint=config.S3_ENDPOINT,
            region=config.S3_REGION,
            bucket=config.S3_BUCKET,
            access_key=config.S3_ACCESS_KEY,
            secret_key=config.S3_SECRET_KEY,
        ))
        await artifacts.ensure_bucket()
        ctx["artifacts"] = artifacts

        # 记忆索引
        memory_indexer = create_memory_indexer(MemoryIndexConfig(
            qdrant_url=config.MEMORY_QDRANT_URL,
            qdrant_api_key=config.MEMORY_QDRANT_API_KEY,
            embedding_url=config.MEMORY_EMBEDDING_URL,
            embedding_api_key=config.MEMORY_EMBEDDING_API_KEY,
            embedding_model=config.MEMORY_EMBEDDING_MODEL,
            embedding_dimension=config.MEMORY_EMBEDDING_DIM,
        ))
        ctx["memory_indexer"] = memory_indexer

        # 控制器映射
        ctx["controllers"] = _controllers

        # 记忆提取队列（arq pool，供 processor 内 enqueue_job("extract", ...) 使用）
        memory_queue = await create_pool(_parse_redis_url(config.REDIS_URL))
        ctx["memory_queue"] = memory_queue

        # 服务容器（供 processor 使用）
        _services.update({
            "config": config,
            "repository": database.repository,
            "redis": redis_conn,
            "publisher": publisher,
            "artifacts": artifacts,
            "controllers": _controllers,
            "checkpointer": checkpointer,
            "memory_queue": memory_queue,
            "memory_index": memory_indexer,
            "memory_indexer": memory_indexer,
            "memory_metrics": observability.metrics,
        })

        # 记忆队列 processor 上下文
        memory_ctx = create_memory_queue_processor(
            repository=database.repository,
            models=config.models,
            index=memory_indexer,
            metrics=observability.metrics,
        )
        ctx["memory_processor_ctx"] = memory_ctx
        # _arq_memory_job 直接从 ctx 读取 repository/models/index/metrics
        ctx.update(memory_ctx)

        # Agent run 处理器（arq job: start / resume-approval / resume-question）
        ctx["run_processor"] = create_run_processor(
            _services,
            telemetry=observability,
            langfuse=langfuse,
        )

        observability.logger.info(
            f"agent worker ready: driver={config.AGENT_DRIVER}, sandbox={config.SANDBOX_RUNTIME}, concurrency={config.WORKER_CONCURRENCY}"
        )

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        """arq Worker 关闭钩子：清理资源。"""
        logger = ctx.get("logger")
        if logger:
            logger.info("worker shutdown started")

        # 停止取消监听
        if task := ctx.get("cancellation_task"):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # abort 所有在途 run
        for controller in _controllers.values():
            controller.abort()

        # 关闭 arq pool
        if memory_queue := ctx.get("memory_queue"):
            await memory_queue.close()

        # 关闭 Redis
        for key in ("redis", "publisher", "cancellation_subscriber"):
            if conn := ctx.get(key):
                await conn.close()

        # 关闭 checkpointer（独立 psycopg 连接池）
        if checkpointer_cm := ctx.get("checkpointer_cm"):
            await checkpointer_cm.__aexit__(None, None, None)

        # 关闭 DB
        if repository := ctx.get("repository"):
            await repository.close()

        # flush 遥测
        if observability := ctx.get("observability"):
            await observability.runtime.shutdown()
        if langfuse := ctx.get("langfuse"):
            await langfuse.shutdown()

        if logger:
            logger.info("worker shutdown completed")

    # arq cron：每小时清理一次孤儿附件
    cron_jobs = [
        cron(_orphan_attachment_cleanup, hour=None, minute=0),  # 每小时整点
    ]

    # Redis 连接配置
    redis_settings = _parse_redis_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:56379")
    )


def main() -> None:
    """直接启动 Worker（非 arq CLI 模式）。

    arq 的 Worker.run() 是同步方法，内部自己管理 event loop，
    不能用 asyncio.run() 包裹。
    """
    config = load_worker_config()
    redis_settings = _parse_redis_url(config.REDIS_URL)

    worker = Worker(
        functions=WorkerSettings.functions,
        cron_jobs=WorkerSettings.cron_jobs,
        redis_settings=redis_settings,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
        max_jobs=config.WORKER_CONCURRENCY,
    )

    # 信号处理
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    worker.run()


if __name__ == "__main__":
    main()
