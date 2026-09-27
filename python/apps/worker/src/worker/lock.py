"""基于 Redis 的会话级分布式锁 — 镜像 apps/worker/src/lock.ts。

同一 session 同时只允许一个 run 执行；持锁期间每 TTL/3 续约一次，
防止长任务被其他 worker 误抢。锁 token 使用随机 UUID，释放/续签
都走 Lua 脚本校验 token，避免误删他人锁。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_LOCK_TTL_MS = 60_000

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


async def with_session_lock(
    redis: Any,
    session_id: str,
    action: Callable[[], Awaitable[T]],
    observe: Callable[[str, float], None] | None = None,
) -> T:
    """获取会话锁并执行 action；锁被占用时立即抛错（不重试，由上游决定是否排队）。"""
    import time

    key = f"agent:session:{session_id}:lock"
    token = str(uuid.uuid4())
    started_at = time.monotonic()
    acquired = await redis.set(key, token, px=_LOCK_TTL_MS, nx=True)
    if not acquired:
        if observe:
            try:
                observe("failure", (time.monotonic() - started_at) * 1000)
            except Exception:
                pass
        raise RuntimeError("Session is already being processed")
    if observe:
        try:
            observe("success", (time.monotonic() - started_at) * 1000)
        except Exception:
            pass

    stop_refresh = asyncio.Event()

    async def _refresh_loop() -> None:
        # 每 TTL/3 续约一次；续约失败只意味着锁将自然过期，不致命。
        while not stop_refresh.is_set():
            try:
                await asyncio.wait_for(stop_refresh.wait(), timeout=_LOCK_TTL_MS / 3_000)
                break
            except TimeoutError:
                pass
            try:
                await redis.eval(_REFRESH_SCRIPT, 1, key, token, str(_LOCK_TTL_MS))
            except Exception:
                pass

    refresh_task = asyncio.create_task(_refresh_loop())
    try:
        return await action()
    finally:
        stop_refresh.set()
        await asyncio.gather(refresh_task, return_exceptions=True)
        try:
            await redis.eval(_RELEASE_SCRIPT, 1, key, token)
        except Exception:
            pass
