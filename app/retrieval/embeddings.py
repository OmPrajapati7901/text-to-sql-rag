"""Embedding client (Ollama, nomic-embed-text).

nomic-embed-text is asymmetric: documents and queries must carry different task prefixes, and
the same configuration must be used at index time and query time or the vector space does not
line up. Getting this wrong degrades recall silently, which is the worst failure mode for a
retrieval layer.

Invalid vectors are rejected, never replaced with zeros: a zero vector is not "no signal", it
is a vector that breaks cosine similarity.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import httpx

from app.contracts.errors import ReasonCode, RetrievalError

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


@dataclass(frozen=True)
class EmbeddingConfig:
    """Recorded alongside every indexed vector so reproducibility is checkable."""

    base_url: str
    model: str
    dimensions: int
    document_prefix: str = DOCUMENT_PREFIX
    query_prefix: str = QUERY_PREFIX

    @property
    def fingerprint(self) -> str:
        return f"{self.model}:{self.dimensions}:{self.document_prefix.strip()}"

    @classmethod
    def from_env(cls) -> EmbeddingConfig:
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
            model=os.getenv("RAG_EMBED_MODEL", "nomic-embed-text"),
            dimensions=int(os.getenv("RAG_EMBED_DIMS", "768")),
        )


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig | None = None, timeout: float = 60.0) -> None:
        self.config = config or EmbeddingConfig.from_env()
        self._timeout = timeout

    def _post(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                f"{self.config.base_url}/api/embed",
                json={"model": self.config.model, "input": inputs},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE,
                f"Embedding service unavailable at {self.config.base_url}: {exc}",
            ) from exc

        vectors = payload.get("embeddings")
        if vectors is None and "embedding" in payload:
            vectors = [payload["embedding"]]
        if not vectors or len(vectors) != len(inputs):
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE,
                f"Embedding service returned {len(vectors or [])} vector(s) for "
                f"{len(inputs)} input(s)",
            )
        return [self._validate(v) for v in vectors]

    def _validate(self, vector: list[float]) -> list[float]:
        if len(vector) != self.config.dimensions:
            raise RetrievalError(
                ReasonCode.UNSUPPORTED_CAPABILITY,
                f"Embedding has {len(vector)} dimensions, index expects "
                f"{self.config.dimensions}",
            )
        if not all(math.isfinite(x) for x in vector):
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE, "Embedding contains non-finite values"
            )
        if not any(vector):
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE,
                "Embedding is all zeros; cosine similarity is undefined. Rejecting rather "
                "than substituting a placeholder vector.",
            )
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._post([self.config.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._post([self.config.query_prefix + text])[0]

    def health(self) -> bool:
        try:
            httpx.get(f"{self.config.base_url}/api/tags", timeout=3.0).raise_for_status()
            return True
        except Exception:
            return False
