"""Redis pub/sub subscription hub — mirrors apps/api/src/stream-subscriptions.ts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)


def _create_redis(url: str, **kwargs: Any) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=False, max_connections=10, **kwargs)


class StreamSubscriptionHub:
    """Reuses a single Redis pub/sub connection per channel."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        channel: str,
        listener: Callable[[], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> Callable[[], None]:
        async with self._lock:
            existing = self._entries.get(channel)
            if existing:
                existing["listeners"].add(listener)
                return lambda: self._release(channel, listener)

            pubsub = _create_redis(self._redis_url).pubsub()
            entry = {
                "pubsub": pubsub,
                "listeners": {listener},
                "task": None,
            }
            self._entries[channel] = entry
            try:
                await pubsub.subscribe(channel)
                entry["task"] = asyncio.create_task(self._listen(channel, pubsub, on_error))
            except Exception:
                self._entries.pop(channel, None)
                try:
                    await pubsub.close()
                except Exception:
                    pass
                raise

            return lambda: self._release(channel, listener)

    async def _listen(
        self,
        channel: str,
        pubsub: redis.client.PubSub,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    entry = self._entries.get(channel)
                    if not entry:
                        break
                    for listener in list(entry["listeners"]):
                        try:
                            listener()
                        except Exception:
                            logger.exception("stream subscription listener failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if on_error:
                try:
                    on_error(e)
                except Exception:
                    pass
            logger.warning("stream subscription error on %s: %s", channel, e)
        finally:
            try:
                await pubsub.close()
            except Exception:
                pass

    def _release(self, channel: str, listener: Callable[[], None]) -> None:
        entry = self._entries.get(channel)
        if not entry:
            return
        entry["listeners"].discard(listener)
        if not entry["listeners"]:
            self._entries.pop(channel, None)
            if entry.get("task"):
                entry["task"].cancel()

    async def close_all(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            if entry.get("task"):
                entry["task"].cancel()
                try:
                    await entry["task"]
                except asyncio.CancelledError:
                    pass
            try:
                await entry["pubsub"].close()
            except Exception:
                pass
