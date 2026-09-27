"""Redis 熔断器存储 — 镜像 apps/worker/src/redis-circuit-breaker.ts。

按 namespace + model 记录失败次数；达到阈值后开启熔断，
半开探测成功后关闭。状态变化通过 AgentTelemetry 上报。
"""

from __future__ import annotations

import time
from typing import Any


class RedisCircuitBreakerStore:
    """agent-core 的 CircuitBreakerStore 接口实现。"""

    def __init__(
        self,
        redis: Any,
        namespace: str,
        failure_threshold: int = 5,
        reset_after_ms: int = 30_000,
        telemetry: Any = None,
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._failure_threshold = failure_threshold
        self._reset_after_ms = reset_after_ms
        self._telemetry = telemetry

    def _key(self, model: str) -> str:
        return f"agent:circuit:{self._namespace}:{model}"

    def _emit(self, model: str, state: str) -> None:
        try:
            if self._telemetry is not None:
                self._telemetry.circuit({"model": model, "state": state})
        except Exception:
            pass

    async def allows(self, model: str) -> bool:
        key = self._key(model)
        opened_at_raw = await self._redis.hget(key, "openedAt")
        opened_at = int(opened_at_raw) if opened_at_raw else 0
        if not opened_at:
            return True
        now_ms = int(time.time() * 1000)
        if now_ms - opened_at < self._reset_after_ms:
            self._emit(model, "rejected")
            return False
        probe = await self._redis.set(
            f"{key}:half-open",
            str(now_ms),
            px=self._reset_after_ms,
            nx=True,
        )
        if probe:
            self._emit(model, "half_open")
            return True
        self._emit(model, "rejected")
        return False

    async def record_success(self, model: str) -> None:
        key = self._key(model)
        # 只有从开启/半开恢复时才记一次 closed，避免每次成功调用都刷状态指标。
        was_open = await self._redis.hget(key, "openedAt")
        if was_open:
            self._emit(model, "closed")
        await self._redis.delete(key, f"{key}:half-open")

    async def record_failure(self, model: str) -> None:
        key = self._key(model)
        failures = await self._redis.hincrby(key, "failures", 1)
        if failures >= self._failure_threshold:
            await self._redis.hset(key, "openedAt", str(int(time.time() * 1000)))
            self._emit(model, "open")
        await self._redis.delete(f"{key}:half-open")
        await self._redis.pexpire(key, self._reset_after_ms * 2)
