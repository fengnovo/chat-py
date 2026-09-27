"""MCP 客户端进程级缓存（base MCP 配置文件驱动、无 per-run 凭证）。

对应 TS 的 getSharedMcpToolsForConfigPath / closeSharedMcpClients。
knowledgeMcp 携带 per-run JWT（绑定 run、5 分钟过期），不能跨 run 复用，不走此缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")

_DEFAULT_TTL_MS = 30 * 60 * 1000
_DEFAULT_CONNECT_TIMEOUT_MS = 15_000


def _now_ms() -> float:
    return time.time() * 1000


class _SharedMcpClientLike(Protocol):
    async def get_tools(self) -> list[Any]: ...
    async def close(self) -> None: ...


class _SharedMcpEntry:
    __slots__ = ("client", "tools", "created_at")

    def __init__(self, client: _SharedMcpClientLike, tools: list[Any], created_at: float) -> None:
        self.client = client
        self.tools = tools
        self.created_at = created_at


class SharedMcpTools:
    tools: list[Any]
    status: str

    def __init__(self, tools: list[Any], status: str) -> None:
        self.tools = tools
        self.status = status


def _has_unresolved_placeholder(text: str, env: dict[str, str | None]) -> bool:
    for match in _ENV_PLACEHOLDER.finditer(text):
        name = match.group(1)
        value = env.get(name, "")
        if not value or not value.strip():
            return True
    return False


def expand_env_placeholders(value: Any, env: dict[str, str | None] | None = None) -> Any:
    """展开 MCP 配置里的 `${ENV_VAR}` 占位（如 API key 放 .env 而不是提交进 git）。

    headers 下引用了未设置/空白变量的条目整条剔除（如 `Bearer ${KEY}` 缺 key 时
    退化成 `Bearer `，trim 后仍有 "Bearer" 字样，不能按空串判断；发出去只会换来 401）。
    """
    if env is None:
        env = dict(os.environ)
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda m: env.get(m.group(1), "") or "", value)
    if isinstance(value, list):
        return [expand_env_placeholders(item, env) for item in value]
    if isinstance(value, dict):
        entries: list[tuple[str, Any]] = []
        for k, v in value.items():
            if k == "headers" and isinstance(v, dict):
                filtered: dict[str, Any] = {}
                for hk, hv in v.items():
                    if isinstance(hv, str) and _has_unresolved_placeholder(hv, env):
                        continue
                    filtered[hk] = expand_env_placeholders(hv, env)
                entries.append((k, filtered))
            else:
                entries.append((k, expand_env_placeholders(v, env)))
        return dict(entries)
    return value


def _build_entry(
    raw: str,
    client_factory: Callable[[dict[str, Any]], _SharedMcpClientLike],
    now: Callable[[], float],
    connect_timeout_ms: int,
) -> asyncio.Future[_SharedMcpEntry]:
    """并发 single-flight：返回 Future 本身实现共享一次建连。"""
    config = expand_env_placeholders(json.loads(raw))
    client = client_factory(config)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_SharedMcpEntry] = loop.create_future()

    async def _connect() -> None:
        try:
            # asyncio.wait_for 兜底"TCP 可连但不响应"的黑洞端点，
            # 超时按连接失败处理（不缓存、可重试）。
            tools = await asyncio.wait_for(client.get_tools(), timeout=connect_timeout_ms / 1000)
            future.set_result(_SharedMcpEntry(client, tools, now()))
        except asyncio.TimeoutError:
            try:
                await client.close()
            except Exception:
                pass
            future.set_exception(
                TimeoutError(
                    f"MCP connect/tools discovery timed out after {connect_timeout_ms}ms"
                )
            )
        except Exception as exc:
            # 连接/发现失败（含超时）时关闭可能半开的 client，再向上抛——调用方不缓存失败条目。
            try:
                await client.close()
            except Exception:
                pass
            future.set_exception(exc)

    loop.create_task(_connect())
    return future


# 缓存 Promise 本身实现 single-flight：并发请求共享同一次建连，不会重复握手。
_shared_clients: dict[str, asyncio.Future[_SharedMcpEntry]] = {}


def _get_now() -> float:
    return _now_ms()


async def get_shared_mcp_tools_for_config_path(
    config_path: str,
    client_factory: Callable[[dict[str, Any]], _SharedMcpClientLike] | None = None,
    ttl_ms: int | None = None,
    connect_timeout_ms: int | None = None,
    now: Callable[[], float] | None = None,
) -> SharedMcpTools:
    raw = Path(config_path).read_text(encoding="utf-8")
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    ttl = ttl_ms if ttl_ms is not None else _DEFAULT_TTL_MS
    timeout = connect_timeout_ms if connect_timeout_ms is not None else _DEFAULT_CONNECT_TIMEOUT_MS
    now_fn = now if now is not None else _get_now

    if client_factory is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        def client_factory(config: dict[str, Any]) -> _SharedMcpClientLike:
            return MultiServerMCPClient(config)

    existing = _shared_clients.get(key)
    if existing is not None:
        try:
            entry = await existing
        except Exception:
            # 失败条目已被清理；重试一次（最多递归一层，重建再失败会直接抛给调用方）。
            if _shared_clients.get(key) is existing:
                _shared_clients.pop(key, None)
            return await get_shared_mcp_tools_for_config_path(
                config_path,
                client_factory=client_factory,
                ttl_ms=ttl_ms,
                connect_timeout_ms=connect_timeout_ms,
                now=now,
            )
        current = _shared_clients.get(key)
        if current is not existing:
            # 已被并发请求重建，直接等新条目。
            built = await current
            return SharedMcpTools(
                tools=built.tools, status=f"{len(built.tools)} tools connected (shared)"
            )
        if now_fn() - entry.created_at <= ttl:
            return SharedMcpTools(
                tools=entry.tools, status=f"{len(entry.tools)} tools connected (shared)"
            )
        # 超龄懒重建（stale-while-revalidate）：新连接握手成功前不动旧 client。
        # 重建失败时继续返回旧工具，并把旧条目时钟拨到当前、TTL 后再试，
        # 避免一次公网抖动/限流直接让整轮对话没有 MCP 工具。
        replacement = _build_entry(raw, client_factory, now_fn, timeout)
        if _shared_clients.get(key) is existing:
            _shared_clients[key] = replacement
            try:
                built = await replacement
                try:
                    await entry.client.close()
                except Exception:
                    pass
                return SharedMcpTools(
                    tools=built.tools,
                    status=f"{len(built.tools)} tools connected (shared, rebuilt)",
                )
            except Exception:
                fallback = asyncio.Future[_SharedMcpEntry]()
                fallback.set_result(_SharedMcpEntry(entry.client, entry.tools, now_fn()))
                if _shared_clients.get(key) is replacement:
                    _shared_clients[key] = fallback
                return SharedMcpTools(
                    tools=entry.tools, status=f"{len(entry.tools)} tools connected (shared, stale)"
                )
        built = await _shared_clients[key]
        return SharedMcpTools(
            tools=built.tools, status=f"{len(built.tools)} tools connected (shared)"
        )

    promise = _build_entry(raw, client_factory, now_fn, timeout)

    def _cleanup_on_fail(fut: asyncio.Future[_SharedMcpEntry]) -> None:
        if fut.cancelled():
            return
        # fut.exception() 同时承担"取回异常"职责，避免 unretrieved exception 告警。
        if _shared_clients.get(key) is fut and fut.exception() is not None:
            _shared_clients.pop(key, None)

    promise.add_done_callback(_cleanup_on_fail)
    _shared_clients[key] = promise
    entry = await promise
    return SharedMcpTools(
        tools=entry.tools, status=f"{len(entry.tools)} tools connected (shared)"
    )


async def close_shared_mcp_clients() -> None:
    """关闭并清空全部共享 client（worker 优雅关闭与测试 reset 使用）。"""
    entries = list(_shared_clients.values())
    _shared_clients.clear()
    results = await asyncio.gather(*entries, return_exceptions=True)
    await asyncio.gather(
        *[
            result.client.close()
            for result in results
            if isinstance(result, _SharedMcpEntry)
        ],
        return_exceptions=True,
    )
