"""FastAPI application entry point — mirrors apps/api/src/app.ts."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from artifacts import ArtifactStoreConfig, S3ArtifactStore
from contracts import AuthContext
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .admin_routes import router as admin_router
from .auth import AuthenticationError, ForbiddenError, create_authenticator
from .auth_routes import router as auth_router
from .config import ApiConfig, load_config
from .observability import ApiObservability, register_observability_middleware
from .oauth_routes import router as oauth_router
from .outbox import RunOutboxDispatcher
from .rate_limit import resolve_rate_limit
from .routes import router as main_router
from .stream_subscriptions import StreamSubscriptionHub

logger = logging.getLogger(__name__)


class RedisPublisher:
    """Thin wrapper around Redis for publish/eval operations."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    async def ping(self) -> None:
        await self._redis.ping()

    async def publish(self, channel: str, message: str) -> None:
        await self._redis.publish(channel, message)

    async def eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        return await self._redis.eval(script, len(keys), *(keys + args))


class ArqQueueWrapper:
    """Thin wrapper around arq Redis for enqueue."""

    def __init__(self, redis_pool: redis.Redis, name: str) -> None:
        self._redis = redis_pool
        self.name = name

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        import json
        payload = json.dumps({"function": function, "args": args, "kwargs": kwargs})
        job_id = _job_id or f"{function}:{time.time()}"
        await self._redis.xadd(self.name, {"payload": payload, "job_id": job_id})
        return {"job_id": job_id}


async def setup_app(config: ApiConfig) -> FastAPI:
    # ── Observability ──────────────────────────────────────────────────────
    from observability import load_observability_config, start_observability
    telemetry_config = load_observability_config(dict(os.environ), service_name="agent-api", service_version=config.API_VERSION)
    runtime = await start_observability(telemetry_config)
    observability = ApiObservability(
        runtime=runtime,
        enabled=telemetry_config.enabled,
        service_version=telemetry_config.service_version,
        exporter="configured" if telemetry_config.enabled else "disabled",
    )

    # ── Database ───────────────────────────────────────────────────────────
    from db import create_database, migrate_database
    database = await create_database(config.database_url)
    await migrate_database(database.pool, migrations_dir=str(Path(__file__).parents[5] / "packages" / "db" / "migrations"))

    # ── Knowledge Repository ───────────────────────────────────────────────
    knowledge_repo = None
    if config.knowledge_embedding_profile:
        from db import KnowledgeRepository, KnowledgeRepositoryOptions
        knowledge_repo = KnowledgeRepository(
            database.pool,
            KnowledgeRepositoryOptions(embedding_profile=config.knowledge_embedding_profile),
        )

    # ── Redis ──────────────────────────────────────────────────────────────
    publisher = RedisPublisher(redis.Redis.from_url(config.REDIS_URL, decode_responses=True))
    queue_redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

    # ── Queues ─────────────────────────────────────────────────────────────
    run_queue = ArqQueueWrapper(queue_redis, "agent-runs")
    knowledge_queue = ArqQueueWrapper(queue_redis, "knowledge-index")
    memory_index_queue = ArqQueueWrapper(queue_redis, "agent-memory-index")
    caption_queue = ArqQueueWrapper(queue_redis, "knowledge-caption") if config.caption_enabled else None

    # ── Artifacts ──────────────────────────────────────────────────────────
    artifacts = S3ArtifactStore(ArtifactStoreConfig(
        endpoint=config.S3_ENDPOINT,
        public_endpoint=config.S3_PUBLIC_ENDPOINT,
        region=config.S3_REGION,
        bucket=config.S3_BUCKET,
        access_key=config.S3_ACCESS_KEY,
        secret_key=config.S3_SECRET_KEY,
    ))
    await artifacts.ensure_bucket()

    # ── Outbox ─────────────────────────────────────────────────────────────
    outbox = RunOutboxDispatcher(
        repository=database.repository,
        queue=run_queue,
        poll_interval_ms=config.OUTBOX_POLL_INTERVAL_MS,
        batch_size=config.OUTBOX_BATCH_SIZE,
        lease_ms=config.OUTBOX_LEASE_MS,
        reconcile_interval_ms=config.OUTBOX_RECONCILE_INTERVAL_MS,
        stale_after_ms=config.OUTBOX_STALE_AFTER_MS,
    )

    # ── Stream Subscriptions ───────────────────────────────────────────────
    stream_subscriptions = StreamSubscriptionHub(config.REDIS_URL)

    # ── Authenticator ──────────────────────────────────────────────────────
    authenticate = await create_authenticator(config, load_membership=database.repository.get_membership_role)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        outbox.start()
        yield
        await outbox.stop()
        await stream_subscriptions.close_all()
        await database.repository.close()
        await runtime.shutdown()

    app = FastAPI(title="Agent API", version=config.API_VERSION, lifespan=lifespan)

    # Store references on app state
    app.state.config = config
    app.state.observability = observability
    app.state.repository = database.repository
    app.state.knowledge_repository = knowledge_repo
    app.state.publisher = publisher
    app.state.run_queue = run_queue
    app.state.knowledge_queue = knowledge_queue
    app.state.memory_index_queue = memory_index_queue
    app.state.caption_queue = caption_queue
    app.state.artifacts = artifacts
    app.state.outbox = outbox
    app.state.stream_subscriptions = stream_subscriptions
    app.state.authenticate = authenticate

    # ── Middleware ─────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.WEB_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["x-agent-run-id", "x-request-id"],
    )

    register_observability_middleware(app, observability)

    # Auth middleware
    PUBLIC_PATHS = {
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/logout",
        "/api/auth/oauth/providers",
    }
    PUBLIC_PREFIXES = (
        "/health/",
        "/api/agent/sessions/",
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Response:
        pathname = request.url.path
        # Health checks
        if pathname.startswith("/health/"):
            return await call_next(request)
        # Preview routes
        if pathname.startswith("/api/agent/sessions/") and "/preview" in pathname:
            return await call_next(request)
        # OAuth routes
        if pathname.startswith("/api/auth/oauth/"):
            return await call_next(request)
        # Public paths
        if pathname in PUBLIC_PATHS:
            if pathname in ("/api/auth/login", "/api/auth/register"):
                try:
                    ip = request.client.host if request.client else "unknown"
                    scope = resolve_rate_limit(pathname, "", ip, config.RATE_LIMIT_REQUESTS, config.KNOWLEDGE_UPLOAD_RATE_LIMIT_REQUESTS)
                    count = await publisher._redis.incr(scope.key)
                    if count == 1:
                        await publisher._redis.pexpire(scope.key, config.RATE_LIMIT_WINDOW_MS)
                    if count > scope.limit:
                        return JSONResponse(status_code=429, content={"error": "rate_limit_exceeded"})
                except Exception:
                    pass
            return await call_next(request)

        # Authenticate
        try:
            auth = await authenticate(
                authorization=request.headers.get("authorization"),
                cookie=request.headers.get("cookie"),
            )
            request.state.auth = auth
        except AuthenticationError:
            return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Authentication required"})

        await database.repository.ensure_identity(auth)

        # Rate limit
        try:
            scope = resolve_rate_limit(pathname, str(auth.tenant_id), str(auth.user_id), config.RATE_LIMIT_REQUESTS, config.KNOWLEDGE_UPLOAD_RATE_LIMIT_REQUESTS)
            count = await publisher._redis.incr(scope.key)
            if count == 1:
                await publisher._redis.pexpire(scope.key, config.RATE_LIMIT_WINDOW_MS)
            ttl = await publisher._redis.pttl(scope.key)
            response = await call_next(request)
            response.headers["x-ratelimit-remaining"] = str(max(0, scope.limit - count))
            if count > scope.limit:
                response.headers["retry-after"] = str(max(1, ttl // 1000))
                return JSONResponse(status_code=429, content={"error": "rate_limit_exceeded"})
            return response
        except Exception:
            return await call_next(request)

    # Error handlers
    @app.exception_handler(AuthenticationError)
    async def auth_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": str(exc)})

    @app.exception_handler(ForbiddenError)
    async def forbidden_error_handler(request: Request, exc: ForbiddenError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": "forbidden", "message": str(exc)})

    from pydantic import ValidationError
    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "issues": exc.errors()})

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    # ── Routes ─────────────────────────────────────────────────────────────
    app.include_router(main_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(oauth_router)
    if knowledge_repo is not None:
        from .knowledge_routes import router as knowledge_router
        from .rag_routes import router as rag_router
        app.include_router(knowledge_router)
        app.include_router(rag_router)

    return app
