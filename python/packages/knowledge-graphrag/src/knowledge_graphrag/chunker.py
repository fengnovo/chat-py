import re
import hashlib
import uuid
from typing import List

from .types import ParsedDocument, TextChunk, ChunkImageRef


def stable_chunk_id(document_id: str, ordinal: int, text: str) -> str:
    """基于 document_id + ordinal + text 生成稳定的 chunk ID（UUID v8 风格）"""
    digest = hashlib.sha256(f"{document_id}\0{ordinal}\0{text}".encode()).digest()
    # 按 TS 版本逻辑：第 7 字节高 4 位置 0100，第 9 字节高 2 位置 10
    b = bytearray(digest[:16])
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    hex_str = b.hex()
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"


def _attach_image_refs(start: int, end: int, refs: List) -> List[ChunkImageRef]:
    """给定 chunk 的字符区间和文档的全部图片引用，挑选 offset 落在区间内的引用"""
    matched: List[ChunkImageRef] = []
    for ref in refs:
        if ref.offset >= start and ref.offset < end:
            matched.append(ChunkImageRef(path=ref.path, alt=ref.alt))
    return matched


def split_into_chunks(document: ParsedDocument, size: int, overlap: int) -> List[TextChunk]:
    """将文档切分为带 heading 路径和图片引用的文本块"""
    if not isinstance(size, int) or size <= 0:
        raise ValueError("size must be positive")
    if not isinstance(overlap, int) or overlap < 0 or overlap >= size:
        raise ValueError("overlap must be less than size")

    chunks: List[TextChunk] = []
    heading_records: List[dict] = []

    if document.mime == "text/markdown":
        # 按行扫描标题位置
        for match in re.finditer(r"([^\r\n]*)(\r\n|\n|\r|$)", document.text):
            line = match.group(1)
            offset = match.start()
            end = offset + len(line)
            heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading_match:
                heading_records.append(
                    {
                        "start": offset,
                        "end": end,
                        "level": len(heading_match.group(1)),
                        "title": heading_match.group(2),
                    }
                )

    start = 0
    text_len = len(document.text)
    while start < text_len:
        end = min(start + size, text_len)
        text = document.text[start:end]

        # 筛选出影响当前 chunk 的 heading 路径
        relevant = [
            h
            for h in heading_records
            if h["end"] <= start or (h["start"] >= start and h["end"] <= end)
        ]
        relevant.sort(key=lambda h: h["start"])
        path: List[str] = []
        for h in relevant:
            level = h["level"]
            if level - 1 < len(path):
                path = path[: level - 1]
            else:
                # 如果层级跳级，用空字符串补齐，保持语义一致
                path = path + [""] * (level - 1 - len(path))
            path.append(h["title"])

        chunks.append(
            TextChunk(
                ordinal=len(chunks),
                text=text,
                start=start,
                end=end,
                heading_path=path,
                image_refs=_attach_image_refs(start, end, document.image_refs),
            )
        )
        if end == text_len:
            break
        start = end - overlap

    return chunks
