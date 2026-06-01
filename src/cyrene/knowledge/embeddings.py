"""Embedding and vector utilities for knowledge base search.

Provides optional vector embeddings via HTTP API (no numpy dependency).
All vector operations degrade gracefully when embeddings are unconfigured.
"""

import math
import os
from array import array

import httpx


def _base_url() -> str:
    """Get embedding base URL from env or config."""
    env_val = os.environ.get("EMBEDDING_BASE_URL", "").strip()
    if env_val:
        return env_val

    try:
        from cyrene import config

        return getattr(config, "EMBEDDING_BASE_URL", "")
    except Exception:
        return ""


def _api_key() -> str:
    """Get embedding API key from env or config."""
    env_val = os.environ.get("EMBEDDING_API_KEY", "").strip()
    if env_val:
        return env_val

    try:
        from cyrene import config

        return getattr(config, "EMBEDDING_API_KEY", "")
    except Exception:
        return ""


def _model() -> str:
    """Get embedding model from env or config."""
    env_val = os.environ.get("EMBEDDING_MODEL", "").strip()
    if env_val:
        return env_val

    try:
        from cyrene import config

        return getattr(config, "EMBEDDING_MODEL", "")
    except Exception:
        return ""


def is_configured() -> bool:
    """Check if all embedding configuration is present."""
    return bool(_base_url() and _api_key() and _model())


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using the configured embedding API.

    Raises an exception if embeddings are not configured or the API call fails.
    """
    if not is_configured():
        raise RuntimeError("Embeddings not configured")

    base_url = _base_url()
    api_key = _api_key()
    model = _model()

    payload = {
        "model": model,
        "input": texts,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/embeddings",
            json=payload,
            headers=headers,
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()

    # Parse embeddings from response
    embeddings = []
    for item in data.get("data", []):
        embedding = item.get("embedding")
        if isinstance(embedding, list):
            embeddings.append(embedding)

    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"Expected {len(texts)} embeddings, got {len(embeddings)}"
        )

    return embeddings


def pack_vector(vec: list[float] | array) -> bytes:
    """Pack a vector into a byte blob."""
    if isinstance(vec, array):
        return vec.tobytes()
    return array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> array:
    """Unpack a byte blob into a vector."""
    vec = array("f")
    vec.frombytes(blob)
    return vec


def cosine(a: array | list[float], b: array | list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 if vectors have different lengths or zero norm.
    """
    if isinstance(a, array):
        a = list(a)
    if isinstance(b, array):
        b = list(b)

    if len(a) != len(b):
        return 0.0

    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot_product / (norm_a * norm_b)
