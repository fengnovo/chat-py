"""DB 层统一 JSON 序列化。

asyncpg 行返回的 ``uuid.UUID`` / ``datetime`` 以及 Pydantic 模型无法被标准库
``json.dumps`` 直接序列化。所有写入 jsonb 列的载荷都必须经过 ``json_dumps``。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def _default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, mode="json")
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json")
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def json_dumps(value: Any) -> str:
    """jsonb 写入专用：ensure_ascii=False（保留中文），兜底 UUID/datetime/Pydantic。"""
    return json.dumps(value, default=_default, ensure_ascii=False)
