"""asyncpg 连接池工厂。

统一注册 json/jsonb codec，让 jsonb 列读出来直接是 ``dict``/``list``
（对齐 Node 版 pg 驱动行为）。

写入侧兼容两种调用方式：
- 传入已序列化的 JSON 字符串（repository 中显式 ``json_dumps(...)``）→ 原样发送；
- 传入 dict/list/含 UUID 的对象 → 用兜底编码器序列化。
"""

from __future__ import annotations

import json as _json
from typing import Any

import asyncpg

from ._json import _default


def _jsonb_encoder(value: Any) -> str:
    if isinstance(value, str):
        # 已序列化的 JSON 文本，避免二次编码
        return value
    return _json.dumps(value, default=_default, ensure_ascii=False)


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=_jsonb_encoder,
            decoder=_json.loads,
            schema="pg_catalog",
        )


async def create_pg_pool(dsn: str, **kwargs: Any) -> asyncpg.Pool:
    """创建带 jsonb 自动编解码的连接池。"""
    return await asyncpg.create_pool(dsn=dsn, init=_init_connection, **kwargs)
