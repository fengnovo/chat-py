import hashlib
import json
from typing import List, Optional
import httpx

from .types import Embedder, EmbeddingProfile


def collection_for_profile(profile, prefix: str = "knowledge") -> str:
    """根据 embedding profile 生成 collection 名称"""
    digest = hashlib.sha256(f"{profile.key}\0{profile.model}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}_{profile.dimension}"


def build_embedding_profile(
    key: str,
    model: str,
    dimension: int,
    collection_prefix: Optional[str] = None,
) -> EmbeddingProfile:
    """构建 EmbeddingProfile，自动生成 collection_name"""
    key = key.strip()
    model = model.strip()
    prefix = (collection_prefix or "knowledge").strip()
    if not key:
        raise ValueError("Embedding profile key is required")
    if not model:
        raise ValueError("Embedding model is required")
    if not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("Embedding dimension must be a positive integer")
    profile_data = EmbeddingProfile(key=key, model=model, dimension=dimension, collection_name="")
    collection_name = collection_for_profile(profile_data, prefix)
    return EmbeddingProfile(key=key, model=model, dimension=dimension, collection_name=collection_name)


class OpenAICompatibleEmbedder(Embedder):
    """OpenAI 兼容的 Embedder 实现，支持批量请求与顺序回退"""

    def __init__(
        self,
        profile: EmbeddingProfile,
        api_key: str,
        base_url: str,
        batch_size: int = 10,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.profile = profile
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("Embedding API key is required")
        base_url = base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("Embedding base URL is required")
        self.batch_size = batch_size
        if not isinstance(self.batch_size, int) or self.batch_size <= 0:
            raise ValueError("Embedding batch size must be a positive integer")
        self.endpoint = f"{base_url}/embeddings"
        self._client = client or httpx.AsyncClient()

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors: List[List[float]] = []
        for offset in range(0, len(texts), self.batch_size):
            batch = texts[offset : offset + self.batch_size]
            batch_num = offset // self.batch_size + 1
            vectors.extend(await self._embed_batch(batch, batch_num))
        return vectors

    async def _embed_batch(self, texts: List[str], batch_number: int) -> List[List[float]]:
        response = await self._client.post(
            self.endpoint,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            json={
                "input": texts,
                "model": self.profile.model,
                "dimensions": self.profile.dimension,
            },
        )
        body = response.text
        if response.status_code >= 400:
            raise RuntimeError(
                f"Embedding batch {batch_number} request failed ({response.status_code}): {_error_message(body)}"
            )

        try:
            data = json.loads(body).get("data")
        except json.JSONDecodeError:
            raise RuntimeError(f"Embedding batch {batch_number} response was not valid JSON")

        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError(
                f"Embedding batch {batch_number} response count mismatch: expected {len(texts)}, received {len(data) if isinstance(data, list) else 0}"
            )

        vectors: List[Optional[List[float]]] = [None] * len(texts)
        # 某些兼容实现（如 dashscope qwen embedding flash）批量返回的 index 恒为 0，
        # 实际按输入顺序返回向量；此时退化为按顺序对应，其余错序仍视为错误。
        first_index = data[0].get("index") if isinstance(data[0], dict) else None
        sequential_fallback = (
            len(texts) > 1
            and first_index is not None
            and all(isinstance(item, dict) and item.get("index") == first_index for item in data)
        )

        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise RuntimeError(f"Embedding batch {batch_number} response was missing an entry")
            index = i if sequential_fallback else item.get("index")
            if not isinstance(index, int) or index < 0 or index >= len(texts) or vectors[index] is not None:
                raise RuntimeError(f"Embedding batch {batch_number} response contained an invalid index")
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or not all(
                isinstance(v, (int, float)) and not (isinstance(v, float) and (v != v)) for v in embedding
            ):
                raise RuntimeError(
                    f"Embedding batch {batch_number} response at index {index} was not a numeric vector"
                )
            if len(embedding) != self.profile.dimension:
                raise RuntimeError(
                    f"Embedding batch {batch_number} dimension mismatch at index {index}: expected {self.profile.dimension}, received {len(embedding)}"
                )
            vectors[index] = embedding

        return vectors  # type: ignore[return-value]

    async def embed_query(self, text: str) -> List[float]:
        result = await self.embed_texts([text])
        if not result:
            raise RuntimeError("Embedding response did not contain a query vector")
        return result[0]


def _error_message(body: str) -> str:
    try:
        parsed = json.loads(body)
        message = parsed.get("error", {}).get("message") or parsed.get("message")
        if isinstance(message, str) and message.strip():
            return message
    except json.JSONDecodeError:
        pass
    return body.strip() or "unknown provider error"


def create_openai_compatible_embedder(
    profile: EmbeddingProfile,
    api_key: str,
    base_url: str,
    batch_size: int = 10,
) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(
        profile=profile,
        api_key=api_key,
        base_url=base_url,
        batch_size=batch_size,
    )


create_embedder = create_openai_compatible_embedder
