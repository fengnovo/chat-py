"""knowledge-service 主入口 — 对应 TS 版 src/main.ts + src/index.ts 的编排部分。

启动顺序：观测 → 配置 → Postgres / Redis(arq) / Qdrant / S3 → 运行时（embedder + pipeline）
→ 索引消费者（arq）→ caption worker（可选）→ MCP HTTP 服务（uvicorn）→ 30s 对账循环。
关闭顺序相反：先停业务面（HTTP、消费者、队列、Redis），再停 PG，最后 flush/关闭遥测。
"""

from __future__ import annotations

import asyncio
import signal
import sys
from types import SimpleNamespace
from typing import Any

import asyncpg
import redis.asyncio as aioredis
import uvicorn
from qdrant_client import AsyncQdrantClient

from artifacts import ArtifactStoreConfig, S3ArtifactStore
from db import KnowledgeRepository, create_pg_pool
from observability import load_observability_config, redact_telemetry_value, start_observability

from .caption_provider import create_null_captioner, create_openai_compatible_captioner
from .caption_worker import CAPTION_QUEUE_NAME, start_caption_worker
from .config import load_config
from .consumer import INDEX_QUEUE_NAME, ArqJobQueue, create_arq_redis, start_consumer
from .extract import create_llm_graph_extractor
from .mcp_server import create_mcp_http_server
from .observability import create_knowledge_observability
from .reconciler import (
    reconcile_caption_jobs,
    reconcile_once,
    reconcile_orphan_caption_assets,
)
from .retriever import ProductionRetrieverDeps, create_retriever
from .runtime import _resolve_chunk_store, create_knowledge_runtime

SERVICE_VERSION = "0.1.0"
_RECONCILE_INTERVAL_S = 30


async def main() -> None:
    # ── 观测 ─────────────────────────────────────────────────────────────
    telemetry_config = load_observability_config(
        None, service_name="knowledge-service", service_version=SERVICE_VERSION
    )
    observability_runtime = await start_observability(telemetry_config)
    obs = create_knowledge_observability(observability_runtime, service_version=SERVICE_VERSION)
    logger = obs.logger
    telemetry = SimpleNamespace(
        tracer=observability_runtime.tracer, metrics=obs.metrics, logger=logger
    )

    config = load_config()

    # ── Postgres ─────────────────────────────────────────────────────────
    pool = await create_pg_pool(dsn=config.postgres_url, min_size=1, max_size=8)
    await pool.fetchval("SELECT 1")
    logger.info("postgres connected", operation="knowledge.startup")

    # ── Redis / 队列（arq）───────────────────────────────────────────────
    redis_client = aioredis.from_url(config.redis_url)
    arq_redis = await create_arq_redis(config.redis_url)
    queue = ArqJobQueue(arq_redis, INDEX_QUEUE_NAME)

    # ── Qdrant ───────────────────────────────────────────────────────────
    qdrant = AsyncQdrantClient(url=config.qdrant_url)
    await qdrant.get_collections()
    vector_store = _resolve_chunk_store(qdrant, config.qdrant_collection_prefix)

    # ── S3 artifacts ─────────────────────────────────────────────────────
    artifacts = S3ArtifactStore(
        ArtifactStoreConfig(
            endpoint=config.s3_endpoint,
            public_endpoint=config.s3_public_endpoint,
            region=config.s3_region,
            bucket=config.s3_bucket,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
        )
    )
    await artifacts.ensure_bucket()

    # ── 仓库 / 图谱抽取 / 运行时 ─────────────────────────────────────────
    repository = KnowledgeRepository(pool)
    extract = create_llm_graph_extractor(
        model=config.extraction_model,
        base_url=config.extraction_base_url,
        api_key=config.extraction_api_key,
        logger=logger,
    )
    runtime = create_knowledge_runtime(
        config,
        {
            "queue": queue,
            "repository": repository,
            "vector_store": vector_store,
            "download": artifacts.get_object_bytes,
            "extract": extract,
            "logger": logger,
        },
    )
    if runtime.embedder is None:
        raise RuntimeError("embedder was not initialized")

    # ── 索引消费者（arq worker，独立连接）─────────────────────────────────
    from arq.connections import RedisSettings

    consumer_deps = SimpleNamespace(
        pipeline=runtime.pipeline,
        repository=repository,
        lease_ms=config.lease_ms,
        logger=logger,
    )
    worker = start_consumer(
        INDEX_QUEUE_NAME,
        RedisSettings.from_dsn(config.redis_url),
        consumer_deps,
        config.concurrency,
        telemetry,
    )

    retriever = create_retriever(
        ProductionRetrieverDeps(
            pool=pool,
            embedder=runtime.embedder,
            vector_store=vector_store,
            repository=repository,
            logger=logger,
            tracer=observability_runtime.tracer,
        )
    )

    # ── VLM caption worker（仅当 captionEnabled 且 baseUrl + apiKey 都齐备时启动）──
    captioner = (
        create_openai_compatible_captioner(
            base_url=config.caption_base_url or "",
            api_key=config.caption_api_key or "",
            model=config.caption_model,
            timeout_ms=config.caption_timeout_ms,
            max_chars=config.caption_max_chars,
            disable_thinking=config.caption_disable_thinking,
        )
        if config.caption_ready
        else create_null_captioner()
    )
    caption_worker_handle: Any | None = None
    caption_queue: ArqJobQueue | None = None
    if config.caption_ready:
        # 对账循环（reconcile_caption_jobs）需要 Queue 句柄才能把 DB 里 queued /
        # 租约过期的任务重新投递 —— 与 TS 版一致，显式持有 captionQueue。
        caption_queue = ArqJobQueue(arq_redis, CAPTION_QUEUE_NAME)

        async def on_caption_ready(input: dict[str, Any]) -> None:
            # caption 写完就入队一条 reindex，让 IndexPipeline 把 caption chunk 加进向量。
            enqueued = await repository.enqueue_reindex_if_attached(
                input["tenantId"], input["kbId"], input["documentId"], "caption_ready"
            )
            if enqueued:
                await queue.add(
                    "reindex-document",
                    {
                        "tenantId": input["tenantId"],
                        "kbId": input["kbId"],
                        "documentId": input["documentId"],
                    },
                )
                logger.info(
                    "reindex enqueued after caption ready",
                    tenant_id=input["tenantId"],
                    kb_id=input["kbId"],
                    document_id=input["documentId"],
                    operation="caption.reindex.enqueue",
                )

        caption_worker_handle = start_caption_worker(
            CAPTION_QUEUE_NAME,
            RedisSettings.from_dsn(config.redis_url),
            SimpleNamespace(
                pool=pool,
                repository=repository,
                download=artifacts.get_object_bytes,
                captioner=captioner,
                logger=logger,
                lease_ms=config.caption_lease_ms,
                model_name=config.caption_model,
                on_caption_ready=on_caption_ready,
            ),
            config.caption_concurrency,
            telemetry,
        )
        logger.info("caption worker started", operation="caption.startup", model=config.caption_model)
    else:
        reason = "disabled" if not config.caption_enabled else "missing_credentials"
        logger.info(
            "caption worker not started; image assets will stay caption_pending",
            operation="caption.startup",
            reason=reason,
            caption_enabled=config.caption_enabled,
            has_base_url=bool(config.caption_base_url),
            has_api_key=bool(config.caption_api_key),
        )

    # ── 就绪探针：只暴露每类依赖的布尔状态，不输出连接串、错误细节等敏感信息 ──
    async def readiness() -> dict[str, bool]:
        async def _probe(fn: Any) -> bool:
            try:
                await fn()
                return True
            except Exception:
                return False

        results = await asyncio.gather(
            _probe(lambda: pool.fetchval("SELECT 1")),
            _probe(redis_client.ping),
            _probe(qdrant.get_collections),
            _probe(artifacts.ping),
        )
        return {
            "postgres": results[0],
            "redis": results[1],
            "qdrant": results[2],
            "s3": results[3],
            "consumer": worker.is_running(),
        }

    # ── MCP HTTP 服务（uvicorn 伺服 ASGI app）─────────────────────────────
    mcp_server = create_mcp_http_server(
        token_secret=config.token_secret,
        retriever=retriever,
        logger=logger,
        telemetry=telemetry,
        readiness=readiness,
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(mcp_server.app, host=config.host, port=config.port, log_level="info")
    )
    serve_task = asyncio.create_task(uvicorn_server.serve())
    logger.info(
        f"listening on :{config.port} (queue {INDEX_QUEUE_NAME}, extraction model {config.extraction_model})",
        operation="knowledge.startup",
        reason="ready",
    )

    # ── 30s 对账循环：DB 是事实，队列只是事件总线 ─────────────────────────
    stop_event = asyncio.Event()

    async def reconcile_loop() -> None:
        while not stop_event.is_set():
            try:
                await reconcile_once(repository, queue, telemetry)
                # caption 对账：找出 queued / 租约过期的 caption 任务重新投递；
                # 孤儿对账：pending 且无任务行的资产补一条 caption job（30s 节流共享）。
                if caption_queue is not None:
                    await reconcile_caption_jobs(repository, caption_queue, logger)
                    await reconcile_orphan_caption_assets(repository, logger)
            except Exception as error:
                logger.error("reconcile loop iteration failed", error=redact_telemetry_value(str(error)))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_RECONCILE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    reconcile_task = asyncio.create_task(reconcile_loop())

    # ── 信号处理与优雅停机 ────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows 等不支持的平台
            pass

    await stop_event.wait()
    logger.info("received shutdown signal, shutting down", operation="knowledge.shutdown")

    uvicorn_server.should_exit = True
    await asyncio.gather(serve_task, return_exceptions=True)
    stop_event.set()
    await asyncio.gather(reconcile_task, return_exceptions=True)
    await asyncio.gather(
        worker.close(),
        caption_worker_handle.close() if caption_worker_handle else asyncio.sleep(0),
        return_exceptions=True,
    )
    if caption_queue is not None:
        # caption_queue 与 queue 共享同一个 arq 连接池，只关一次
        caption_queue = None
    try:
        await queue.close()
    except Exception:
        pass
    await redis_client.aclose()  # type: ignore[attr-defined]
    await asyncio.gather(pool.close(), return_exceptions=True)
    try:
        await observability_runtime.shutdown()
    except Exception as error:
        logger.error(
            "observability shutdown failed",
            error=redact_telemetry_value(str(error)),
            operation="knowledge.shutdown",
        )
        sys.exit(1)


def run() -> None:
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 — 顶层兜底，打印后退出
        print(f"[knowledge-service] fatal: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
