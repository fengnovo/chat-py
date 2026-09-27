import gzip
import hashlib
from typing import Any, List

from .parser import parse_document, is_supported_mime
from .chunker import split_into_chunks, stable_chunk_id


def sha256_hex(data: bytes) -> str:
    """计算字节流的 SHA-256 hex"""
    return hashlib.sha256(data).hexdigest()


def assert_document_bytes(data: bytes, expected_hash: str, expected_size: int, mime: str) -> None:  # noqa: ARG001
    """校验文档字节流的大小与哈希是否匹配"""
    if len(data) != expected_size:
        raise ValueError("Document size mismatch")
    if sha256_hex(data).lower() != expected_hash.lower():
        raise ValueError("Document hash mismatch")


def build_image_caption_chunks(
    document_id: str,  # noqa: ARG001
    assets: List[dict],
) -> List[dict]:
    """
    把挂在本文档下的图片 caption 转成合成 chunk，参与向量召回。
    用 9000+ 序数避开 md 文本 chunk；heading 形如 "图片 · <filename>"，方便 UI 区分。
    image_refs 用 kb 级路径（rel_path），与文本 chunk 走同一套反查逻辑。
    """
    result: List[dict] = []
    for index, asset in enumerate(assets):
        caption = asset.get("caption", "")
        if not isinstance(caption, str) or not caption.strip():
            continue
        caption = caption.strip()
        alt = caption if len(caption) <= 80 else caption[:77] + "…"
        result.append(
            {
                "ordinal": 9000 + index,
                "text": f"图片描述：{caption}",
                "start": 2**53 - 1 - index,  # 避免与文本 chunk 的 [start, end) 区间重叠
                "end": 2**53 - 1,
                "heading_path": ["图片", asset["name"]],
                "image_refs": [{"path": asset["rel_path"], "alt": alt}],
            }
        )
    return result


class IndexPipeline:
    """索引流水线"""

    def __init__(self, deps: Any):
        self.deps = deps

    async def run(self, job: Any) -> None:
        d = self.deps
        try:
            async def guard(stage: str) -> None:
                ok = await d.repository.mark_index_stage(
                    job.tenant_id, job.id, job.lease_token or "", stage
                )
                if ok is False:
                    raise RuntimeError("Index job lease lost")

            await guard("parsing")
            downloaded = await d.download(job.object_key)
            data = gzip.decompress(downloaded) if job.content_encoding == "gzip" else downloaded
            assert_document_bytes(data, job.content_hash, job.size_bytes, job.mime)
            if not is_supported_mime(job.mime):
                raise ValueError(f"Unsupported MIME type: {job.mime}")
            parsed = parse_document(data, job.mime)

            await guard("chunking")
            text_chunks = split_into_chunks(parsed, size=job.chunk_size, overlap=job.chunk_overlap)

            # 拉本 doc 已 ready 的图片 caption，合并为同一份 chunk 数组参与 embedding
            caption_assets: List[dict] = []
            if hasattr(d.repository, "list_document_assets_for_indexing"):
                caption_assets = await d.repository.list_document_assets_for_indexing(
                    job.tenant_id,
                    job.kb_id,
                    job.document_id,
                    caption_status="ready",
                )
            caption_chunks = build_image_caption_chunks(job.document_id, caption_assets)
            chunks = text_chunks + caption_chunks

            vectors = await d.embedder.embed_texts([c["text"] if isinstance(c, dict) else c.text for c in chunks])

            await guard("persisting")
            await d.vector_store.ensure_collection(d.embedder.profile)
            await d.vector_store.delete_by_document(job.tenant_id, job.kb_id, job.document_id)

            points: List[dict] = []
            for i, c in enumerate(chunks):
                chunk_id = stable_chunk_id(
                    job.document_id,
                    c["ordinal"] if isinstance(c, dict) else c.ordinal,
                    c["text"] if isinstance(c, dict) else c.text,
                )
                payload = {
                    "tenant_id": job.tenant_id,
                    "kb_id": job.kb_id,
                    "document_id": job.document_id,
                    "chunk_id": chunk_id,
                    "image_refs": c["image_refs"] if isinstance(c, dict) else c.image_refs,
                    "chunk_source": "image_caption" if (c["ordinal"] if isinstance(c, dict) else c.ordinal) >= 9000 else "document_text",
                }
                points.append({"id": chunk_id, "vector": vectors[i], "payload": payload})

            await d.vector_store.upsert(points)

            if hasattr(d.repository, "replace_document_chunks"):
                chunk_records = []
                for i, c in enumerate(chunks):
                    ordinal = c["ordinal"] if isinstance(c, dict) else c.ordinal
                    text = c["text"] if isinstance(c, dict) else c.text
                    heading_path = c["heading_path"] if isinstance(c, dict) else c.heading_path
                    image_refs = c["image_refs"] if isinstance(c, dict) else c.image_refs
                    chunk_records.append(
                        {
                            "id": points[i]["id"],
                            "ordinal": ordinal,
                            "text": text,
                            "heading": " / ".join(heading_path) if heading_path else "",
                            "vector_point_id": points[i]["id"],
                            "metadata": {
                                "headingPath": heading_path,
                                "imageRefs": image_refs,
                                "source": "image_caption" if ordinal >= 9000 else "document_text",
                            },
                            "tokenCount": len(text),
                        }
                    )
                await d.repository.replace_document_chunks(
                    job.tenant_id, job.kb_id, job.document_id, chunk_records
                )

            graph = {"entities": [], "relationships": []}
            # 图谱抽取只跑文本 chunk；caption chunk 是从图片里来的，模型看不到，再抽会重复同一组实体并打乱权重。
            for chunk in text_chunks:
                chunk_id = stable_chunk_id(job.document_id, chunk.ordinal, chunk.text)
                extracted = await d.extract(chunk.text)
                for entity in extracted.get("entities", []):
                    e = dict(entity)
                    e["chunkIds"] = list(e.get("chunkIds", [])) + [chunk_id]
                    graph["entities"].append(e)
                for rel in extracted.get("relationships", []):
                    r = dict(rel)
                    r["chunkIds"] = list(r.get("chunkIds", [])) + [chunk_id]
                    graph["relationships"].append(r)

            await d.repository.replace_document_graph(
                job.tenant_id, job.kb_id, job.document_id, graph
            )
            completed = await d.repository.complete_index_job(
                job.tenant_id, job.id, job.lease_token or "", len(chunks)
            )
            if completed is False:
                raise RuntimeError("Index job lease lost")
        except Exception as error:
            await d.repository.fail_index_job(
                job.tenant_id, job.id, job.lease_token or "", error
            )
            raise
