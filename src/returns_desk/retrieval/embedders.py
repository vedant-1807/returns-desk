"""Text embedders.

Three interchangeable ways to turn text into the 768 numbers policy_chunks stores:

    hash        deterministic bag-of-words hashing; no model, no network; tests and CI
    ollama      a local embedding model through Ollama's OpenAI-compatible endpoint
    openrouter  a hosted embedding model through OpenRouter

Vectors from different embedders cannot be compared with each other. Each embedder has a
`model_id`, every stored chunk records the one that produced it, and every search filters
on it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx2

from returns_desk.config import Settings

EMBEDDING_DIM = 768

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for", "from",
        "has", "have", "how", "i", "if", "in", "is", "it", "its", "of", "on", "or", "our",
        "so", "that", "the", "their", "then", "there", "these", "this", "to", "was", "we",
        "what", "when", "which", "who", "will", "with", "you", "your", "my", "me", "am",
    }
)  # fmt: skip


class Embedder(Protocol):
    model_id: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        # A text with no usable words still needs a valid unit vector for cosine distance.
        fallback = [0.0] * len(vector)
        fallback[0] = 1.0
        return fallback
    return [x / norm for x in vector]


class HashEmbedder:
    """Feature-hashed bag of words. Texts that share words land close together, which is
    enough to make retrieval tests meaningful without any model."""

    model_id = "hash-bow-768"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for word in _WORD.findall(text.lower()):
            if word in _STOPWORDS:
                continue
            # Fold simple plurals so "returns" and "return" share a bucket.
            if len(word) > 3 and word.endswith("s"):
                word = word[:-1]
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        return _normalize(vector)


# Some embedding models expect a short instruction in front of the text. Using the wrong
# one, or none, quietly costs retrieval quality.
_PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "baai/bge-base-en-v1.5": ("", "Represent this sentence for searching relevant passages: "),
}


class OpenAICompatEmbedder:
    """Any server with an OpenAI-style POST /embeddings: Ollama locally, OpenRouter hosted."""

    def __init__(
        self,
        *,
        route: str,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model_id = f"{route}/{model}"
        self._model = model
        self._document_prefix, self._query_prefix = _PREFIXES.get(model.split(":")[0], ("", ""))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds
        )

    def close(self) -> None:
        self._client.close()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self._document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        response = self._client.post("/embeddings", json={"model": self._model, "input": inputs})
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        if len(vectors) != len(inputs):
            raise RuntimeError(f"{self.model_id} returned {len(vectors)} vectors for {len(inputs)}")
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"{self.model_id} returned {len(vector)} dimensions; the schema stores "
                    f"{EMBEDDING_DIM}. Pick a 768-dimension model."
                )
        return [_normalize(vector) for vector in vectors]


def build_embedder(settings: Settings) -> Embedder:
    kind = settings.embedder
    if kind == "hash":
        return HashEmbedder()
    if kind == "ollama":
        return OpenAICompatEmbedder(
            route="ollama",
            base_url=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
        )
    if kind == "openrouter":
        if not settings.openrouter_api_key:
            raise RuntimeError("RD_EMBEDDER=openrouter needs RD_OPENROUTER_API_KEY")
        return OpenAICompatEmbedder(
            route="openrouter",
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_embedding_model,
            api_key=settings.openrouter_api_key,
        )
    raise RuntimeError(f"unknown RD_EMBEDDER {kind!r}; use hash, ollama or openrouter")


def to_pgvector(vector: list[float]) -> str:
    """The text form pgvector parses: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
