"""In-memory retrieval provider: BM25 + cosine over numpy.

Used as the default provider and as one half of the two-implementation contract suite. It
enforces exactly the same discovery restriction semantics as the OpenSearch adapter — that
equivalence is what the contract tests check.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from app.contracts.errors import ReasonCode, RetrievalError
from app.contracts.retrieval import (
    Candidate,
    CompletionStatus,
    ProviderCapabilities,
    ProviderDiagnostics,
    SearchRequest,
    SearchResponse,
)
from app.contracts.scope import DiscoveryRestriction, TrustedScope
from app.retrieval.fusion import reciprocal_rank_fusion

_TOKEN = re.compile(r"[a-z0-9_]+")
K1, B = 1.5, 0.75


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class _Indexed:
    doc: dict
    tokens: list[str]
    length: int
    vector: np.ndarray | None


@dataclass
class InMemoryProvider:
    name: str = "inmemory"
    embedding_dimensions: int | None = None
    _docs: dict[str, _Indexed] = field(default_factory=dict, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avg_len: float = 0.0
    _snapshots: set[str] = field(default_factory=set, repr=False)
    _embedder: object | None = None

    # -- publisher side --------------------------------------------------

    def index(self, documents: list[dict]) -> None:
        for doc in documents:
            text = f"{doc['title']} {' '.join(doc.get('aliases', []))} {doc['content']}"
            tokens = tokenize(text)
            vector = doc.get("embedding")
            self._docs[doc["chunk_id"]] = _Indexed(
                doc=doc,
                tokens=tokens,
                length=len(tokens),
                vector=np.asarray(vector, dtype=np.float32) if vector else None,
            )
            self._snapshots.add(doc["snapshot_id"])
            for token in set(tokens):
                self._df[token] += 1
        lengths = [d.length for d in self._docs.values()]
        self._avg_len = sum(lengths) / len(lengths) if lengths else 0.0
        dims = {len(d.vector) for d in self._docs.values() if d.vector is not None}
        if len(dims) > 1:
            raise RetrievalError(
                ReasonCode.UNSUPPORTED_CAPABILITY,
                f"Index contains vectors of mixed dimensions {sorted(dims)}; vectors from "
                f"different embedding spaces must not share an index",
            )
        self.embedding_dimensions = next(iter(dims), None)

    def retire(self, snapshot_id: str) -> None:
        for chunk_id in [k for k, v in self._docs.items() if v.doc["snapshot_id"] == snapshot_id]:
            del self._docs[chunk_id]
        self._snapshots.discard(snapshot_id)

    def is_ready(self, snapshot_id: str, expected_count: int) -> bool:
        actual = sum(1 for d in self._docs.values() if d.doc["snapshot_id"] == snapshot_id)
        return actual == expected_count

    # -- provider side ---------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_keyword=True,
            supports_vector=any(d.vector is not None for d in self._docs.values()),
            supports_domain_filter=True,
            supports_table_scoped_columns=True,
            supports_denied_object_filter=True,
            embedding_dimensions=self.embedding_dimensions,
        )

    async def health(self) -> bool:
        return bool(self._docs)

    def set_embedder(self, embedder) -> None:
        self._embedder = embedder

    def _permitted(self, doc: dict, restriction: DiscoveryRestriction) -> bool:
        """The trusted restriction, applied inside the search boundary — before anything
        reaches a reranker or a model."""
        if doc["snapshot_id"] != restriction.snapshot_id:
            return False
        if doc["publication_status"] != restriction.publication_status:
            return False
        if restriction.access_scopes and doc["access_scope"] not in restriction.access_scopes:
            return False
        if (
            restriction.allowed_domains is not None
            and doc["domain"] not in restriction.allowed_domains
        ):
            return False
        for denied in restriction.denied_object_ids:
            target = denied[:-2] if denied.endswith(".*") else denied
            if doc["object_id"] == target or (
                denied.endswith(".*") and doc["object_id"].startswith(target + ".")
            ):
                return False
        return True

    def _bm25(self, query_tokens: list[str], pool: list[str]) -> list[str]:
        n = len(pool) or 1
        # Filters are applied before ranking, so document frequency must describe that same
        # candidate pool. Using the whole-index frequency with a two-document type filter can
        # make IDF negative and erase an otherwise exact keyword match.
        pool_df: Counter = Counter()
        for chunk_id in pool:
            pool_df.update(set(self._docs[chunk_id].tokens))
        scores: dict[str, float] = {}
        for chunk_id in pool:
            entry = self._docs[chunk_id]
            counts = Counter(entry.tokens)
            score = 0.0
            for token in query_tokens:
                tf = counts.get(token, 0)
                if not tf:
                    continue
                df = pool_df.get(token, 0) or 1
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + K1 * (1 - B + B * entry.length / (self._avg_len or 1))
                score += idf * (tf * (K1 + 1)) / denom
            if score > 0:
                scores[chunk_id] = score
        return sorted(scores, key=lambda k: (-scores[k], k))

    def _vector(self, query_vector: np.ndarray, pool: list[str]) -> list[str]:
        scored: dict[str, float] = {}
        qnorm = float(np.linalg.norm(query_vector)) or 1.0
        for chunk_id in pool:
            vector = self._docs[chunk_id].vector
            if vector is None:
                continue
            dnorm = float(np.linalg.norm(vector)) or 1.0
            scored[chunk_id] = float(query_vector @ vector) / (qnorm * dnorm)
        return sorted(scored, key=lambda k: (-scored[k], k))

    async def search(self, request: SearchRequest, scope: TrustedScope) -> SearchResponse:
        # The request must search the snapshot the trusted scope pins. Anything else would
        # let a caller read a catalog version their authorization was not derived against.
        if request.snapshot_id != scope.snapshot_id:
            raise RetrievalError(
                ReasonCode.SNAPSHOT_NOT_READY,
                f"Request pins snapshot {request.snapshot_id!r} but the trusted scope pins "
                f"{scope.snapshot_id!r}",
            )
        restriction = scope.discovery
        if restriction.is_empty():
            # An empty restriction admits nothing. Fail closed rather than returning all.
            return SearchResponse(
                candidates=(),
                diagnostics=ProviderDiagnostics(
                    provider=self.name, status=CompletionStatus.COMPLETE
                ),
            )
        if request.snapshot_id not in self._snapshots:
            raise RetrievalError(
                ReasonCode.SNAPSHOT_NOT_READY,
                f"Snapshot {request.snapshot_id!r} is not indexed by {self.name}",
            )

        pool = [
            chunk_id
            for chunk_id, entry in self._docs.items()
            if self._permitted(entry.doc, restriction)
            and (
                not request.object_types
                or entry.doc["object_type"] in {t.value for t in request.object_types}
            )
            and (not request.table_ids or entry.doc.get("table_id") in request.table_ids)
            and (not request.domains or entry.doc["domain"] in request.domains)
        ]

        query_tokens = tokenize(request.search_text)
        keyword = self._bm25(query_tokens, pool)[: request.candidate_budget]

        vector_ranked: list[str] = []
        if self._embedder is not None:
            try:
                qv = np.asarray(self._embedder.embed_query(request.search_text), dtype=np.float32)
                vector_ranked = self._vector(qv, pool)[: request.candidate_budget]
            except RetrievalError:
                raise

        rankings = [r for r in (keyword, vector_ranked) if r]
        fused = reciprocal_rank_fusion(rankings) if rankings else []

        candidates = tuple(
            Candidate(
                chunk_id=chunk_id,
                object_id=self._docs[chunk_id].doc["object_id"],
                object_type=self._docs[chunk_id].doc["object_type"],
                table_id=self._docs[chunk_id].doc.get("table_id"),
                source_version=self._docs[chunk_id].doc["source_version"],
                snapshot_id=self._docs[chunk_id].doc["snapshot_id"],
                title=self._docs[chunk_id].doc["title"],
                excerpt=self._docs[chunk_id].doc["content"][:600],
                source_ref=self._docs[chunk_id].doc["source_ref"],
                rank=i,
            )
            for i, chunk_id in enumerate(fused[: request.candidate_budget], start=1)
        )
        return SearchResponse(
            candidates=candidates,
            diagnostics=ProviderDiagnostics(
                provider=self.name,
                status=CompletionStatus.COMPLETE,
                keyword_hits=len(keyword),
                vector_hits=len(vector_ranked),
            ),
        )
