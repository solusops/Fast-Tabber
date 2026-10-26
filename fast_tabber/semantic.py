"""Local EmbeddingGemma text/image inference and vector operations."""

import json
import math
from pathlib import Path

import httpx
from sklearn.cluster import DBSCAN

from fast_tabber.preflight import EMBEDDING, OLLAMA, require_models


def validate_embeddings(payload: dict, count: int, dimensions: int | None = None):
    vectors = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise RuntimeError("EmbeddingGemma returned the wrong number of vectors")
    sizes = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(sizes) != 1 or len(vectors) != sum(isinstance(vector, list) for vector in vectors):
        raise RuntimeError("EmbeddingGemma returned invalid vector dimensions")
    size = next(iter(sizes))
    if not size or dimensions is not None and size != dimensions or any(
            not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector) or not any(vector)
            for vector in vectors):
        raise RuntimeError("EmbeddingGemma returned invalid vectors")
    return vectors


def cosine(a: list[float], b: list[float]):
    if len(a) != len(b):
        raise ValueError("Embedding dimensions differ")
    denominator = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


def cluster_vectors(vectors: list[list[float]], distance: float = 0.28):
    if len(vectors) < 2:
        return [-1] * len(vectors)
    return DBSCAN(eps=distance, min_samples=2, metric="cosine", n_jobs=1).fit_predict(vectors).tolist()


async def embedding_digest(state: Path, client_factory=httpx.AsyncClient):
    try:
        recorded = json.loads((state / "models.json").read_text(encoding="utf-8-sig"))["models"][EMBEDDING]
        async with client_factory(base_url=OLLAMA, timeout=180, trust_env=False) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            digest = require_models(response.json())[EMBEDDING]
    except httpx.HTTPError as error:
        raise RuntimeError(f"Local EmbeddingGemma is unavailable ({type(error).__name__})") from error
    except (OSError, KeyError, ValueError) as error:
        raise RuntimeError("Embedding verification is missing or invalid; use onboarding(action='verify')") from error
    if digest != recorded:
        raise RuntimeError("Installed EmbeddingGemma digest differs from preflight; run tabber.ps1 check")
    return digest


async def embed_texts(texts: list[str | dict], state: Path, expected_digest: str | None = None,
                      client_factory=httpx.AsyncClient):
    if not texts:
        return [], await embedding_digest(state, client_factory)
    try:
        recorded = json.loads((state / "models.json").read_text(encoding="utf-8-sig"))
        dimensions = recorded.get("embedding_dimensions")
        async with client_factory(base_url=OLLAMA, timeout=180, trust_env=False) as client:
            tags = await client.get("/api/tags")
            tags.raise_for_status()
            digest = require_models(tags.json())[EMBEDDING]
            if digest != recorded["models"][EMBEDDING] or expected_digest and digest != expected_digest:
                raise RuntimeError("Installed EmbeddingGemma digest changed; run tabber.ps1 check")
            vectors = []
            for offset in range(0, len(texts), 32):
                batch = texts[offset:offset + 32]
                response = await client.post("/api/embed", json={"model": EMBEDDING, "input": batch, "truncate": False})
                response.raise_for_status()
                vectors.extend(validate_embeddings(response.json(), len(batch), dimensions))
    except httpx.HTTPError as error:
        raise RuntimeError(f"Local EmbeddingGemma inference failed ({type(error).__name__})") from error
    except (OSError, KeyError, ValueError, TypeError) as error:
        if isinstance(error, (RuntimeError,)): raise
        raise RuntimeError("Verified model report or embedding output is invalid") from error
    return vectors, digest
