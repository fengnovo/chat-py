from typing import Literal, List, Optional, Set
from pydantic import BaseModel, Field


# 支持的文档 MIME 类型
DocumentMime = Literal[
    "text/plain",
    "text/markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


class ParsedSection(BaseModel):
    """解析出的文档章节信息"""
    level: int
    title: str


class ParsedImageRef(BaseModel):
    """md 中出现的图片引用：相对路径 + alt 文本 + 在原文中的字符 offset"""
    path: str = Field(description="相对路径，已去除 ./ 前缀（统一使用 basename 或子目录形式）")
    alt: str = Field(description="![alt](path) 的 alt 文本，可为空")
    offset: int = Field(description="图片引用 `![](...)` 起始 '!' 字符在原文 text 中的字符 offset")


class ParsedDocument(BaseModel):
    """解析后的文档"""
    text: str
    mime: DocumentMime
    sections: List[ParsedSection] = Field(default_factory=list)
    image_refs: List[ParsedImageRef] = Field(default_factory=list, description="markdown 文档中收集到的全部图片引用及其 offset")


class ChunkImageRef(BaseModel):
    """chunk 上挂的图片引用列表，相对路径形式"""
    path: str
    alt: str


class TextChunk(BaseModel):
    """文本块"""
    ordinal: int
    text: str
    start: int = Field(description="chunk 对应原文 text 中的 [start, end) 字符区间，用于把图片引用挂到正确的 chunk 上")
    end: int
    heading_path: List[str] = Field(default_factory=list)
    image_refs: List[ChunkImageRef] = Field(default_factory=list, description="落在本 chunk 字符区间内的 markdown 图片引用")


class GraphEntity(BaseModel):
    """图谱实体"""
    name: str
    key: Optional[str] = None


class GraphRelationship(BaseModel):
    """图谱关系"""
    source: str
    target: str
    type: str


class GraphExtraction(BaseModel):
    """从文本中抽取的实体和关系"""
    entities: List[GraphEntity] = Field(default_factory=list)
    relationships: List[GraphRelationship] = Field(default_factory=list)


class GraphLimits(BaseModel):
    """图谱遍历限制"""
    max_hops: int
    max_fanout: int
    max_relations: int


class GraphRelation(BaseModel):
    """图谱关系（含遍历元信息）"""
    id: str
    source: str
    target: str
    type: str
    source_chunk_ids: List[str]
    hop: int


class GraphTraversal(BaseModel):
    """图谱遍历结果"""
    entity_keys: List[str]
    relations: List[GraphRelation]
    chunk_ids: List[str]


class GraphStore:
    """图谱存储接口（抽象基类），非 Pydantic model — 子类自行管理初始化。"""

    def add_extraction(self, document_id: str, chunk_id: str, extraction: GraphExtraction) -> None:
        raise NotImplementedError

    def entity_keys_for_chunks(self, chunk_ids: List[str]) -> Set[str]:
        raise NotImplementedError

    def traverse(self, seed_keys: List[str], limits: GraphLimits) -> GraphTraversal:
        raise NotImplementedError

    def remove_document(self, document_id: str) -> None:
        raise NotImplementedError


class EmbeddingProfile(BaseModel):
    """Embedding 配置"""
    key: str
    model: str
    dimension: int
    collection_name: str


class Embedder:
    """Embedder 接口（抽象基类），非 Pydantic model — 子类自行管理初始化。"""

    profile: EmbeddingProfile

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    async def embed_query(self, text: str) -> List[float]:
        raise NotImplementedError


class VectorHit(BaseModel):
    """向量召回结果"""
    chunk_id: str
    score: float
    source_chunk_ids: Optional[List[str]] = None


class CandidateEvidence(BaseModel):
    """融合后的候选证据"""
    chunk_id: str
    score: float
    via: Literal["vector", "graph", "vector+graph"]
    source_chunk_ids: List[str]
