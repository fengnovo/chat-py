"""knowledge-service — GraphRAG 检索 / 索引消费者 / MCP 服务。

TS 版 apps/knowledge-service 的 Python 迁移：
- BullMQ → arq（队列名与任务载荷保持一致）
- 检索与索引复用 workspace 内的 knowledge-graphrag 包
- MCP 服务基于官方 mcp Python SDK（FastMCP，Streamable HTTP）
"""

from __future__ import annotations

from .config import KnowledgeServiceConfig, load_config

__all__ = ["KnowledgeServiceConfig", "load_config"]
