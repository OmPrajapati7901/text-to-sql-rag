"""OpenSearch retrieval provider.

Deployment specifics — index mappings, the client, and query DSL — stay inside this adapter.
Nothing OpenSearch-shaped crosses the RetrievalProvider boundary.

Two details that matter and are easy to get wrong:

* The authorization filter goes INSIDE the knn clause. A trailing `post_filter` prunes after
  neighbour selection, so under a restrictive scope you can get far fewer usable neighbours
  than requested — silently.
* An accepted HTTP response does not mean every shard answered. Timeouts and shard failures
  are reported as PARTIAL/TIMED_OUT rather than being passed off as a short COMPLETE list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

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

INDEX_PREFIX = "enterprise-metadata"
SOURCE_FIELDS = [
    "chunk_id", "object_id", "object_type", "table_id", "domain", "access_scope",
    "snapshot_id", "source_version", "source_hash", "publication_status",
    "title", "aliases", "content", "source_ref",
]


def index_name(snapshot_id: str) -> str:
    """One physical index per snapshot. Published snapshots are immutable, and a new
    embedding space needs a new index rather than mixed vectors in an old one."""
    return f"{INDEX_PREFIX}-{snapshot_id}"


def index_body(dimensions: int) -> dict[str, Any]:
    """Explicit mappings. Every field used for filtering is indexed: a policy attribute that
    lives only in an unindexed object cannot be filtered on."""
    return {
        "settings": {"index.knn": True},
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "object_id": {"type": "keyword"},
                "object_type": {"type": "keyword"},
                "table_id": {"type": "keyword"},
                "domain": {"type": "keyword"},
                "access_scope": {"type": "keyword"},
                "snapshot_id": {"type": "keyword"},
                "source_version": {"type": "keyword"},
                "source_hash": {"type": "keyword"},
                "publication_status": {"type": "keyword"},
                "source_ref": {"type": "keyword"},
                "title": {"type": "text", "fields": {"exact": {"type": "keyword"}}},
                "aliases": {"type": "text", "fields": {"exact": {"type": "keyword"}}},
                "content": {"type": "text"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                    },
                },
            },
        },
    }


def authorization_filter(restriction: DiscoveryRestriction) -> dict[str, Any]:
    """Translate the normalized restriction into OpenSearch DSL.

    If a restriction cannot be faithfully represented, this raises rather than returning a
    weaker filter.
    """
    must: list[dict] = [
        {"term": {"snapshot_id": restriction.snapshot_id}},
        {"term": {"publication_status": restriction.publication_status}},
    ]
    if restriction.access_scopes:
        must.append({"terms": {"access_scope": sorted(restriction.access_scopes)}})
    if restriction.allowed_domains is not None:
        must.append({"terms": {"domain": sorted(restriction.allowed_domains)}})

    must_not: list[dict] = []
    for denied in sorted(restriction.denied_object_ids):
        if denied.endswith(".*"):
            must_not.append({"prefix": {"object_id": denied[:-1]}})
        else:
            must_not.append({"term": {"object_id": denied}})

    return {"bool": {"filter": must, "must_not": must_not}}


@dataclass
class OpenSearchProvider:
    url: str = field(default_factory=lambda: os.getenv("OPENSEARCH_URL", "http://localhost:9200"))
    name: str = "opensearch"
    embedding_dimensions: int = 768
    _embedder: object | None = None
    _client: Any = field(default=None, repr=False)

    def client(self):
        if self._client is None:
            from opensearchpy import OpenSearch

            self._client = OpenSearch(hosts=[self.url], timeout=30)
        return self._client

    def set_embedder(self, embedder) -> None:
        self._embedder = embedder

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_keyword=True,
            supports_vector=self._embedder is not None,
            supports_domain_filter=True,
            supports_table_scoped_columns=True,
            supports_denied_object_filter=True,
            embedding_dimensions=self.embedding_dimensions,
        )

    async def health(self) -> bool:
        try:
            return bool(self.client().info())
        except Exception:
            return False

    # -- publisher side --------------------------------------------------

    def publish(self, documents: list[dict], snapshot_id: str) -> dict[str, Any]:
        client = self.client()
        index = index_name(snapshot_id)
        if not client.indices.exists(index=index):
            client.indices.create(index=index, body=index_body(self.embedding_dimensions))

        bulk: list[dict] = []
        for doc in documents:
            bulk.append({"index": {"_index": index, "_id": doc["chunk_id"]}})
            bulk.append(doc)
        response = client.bulk(body=bulk, refresh=True)

        # An accepted bulk request does not mean every item was indexed.
        failures = [
            item["index"] for item in response.get("items", [])
            if item.get("index", {}).get("error")
        ]
        if failures:
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE,
                f"{len(failures)} document(s) failed to index, first: {failures[0]}",
            )
        return {"index": index, "indexed": len(documents)}

    def is_ready(self, snapshot_id: str, expected_count: int) -> bool:
        try:
            client = self.client()
            index = index_name(snapshot_id)
            if not client.indices.exists(index=index):
                return False
            client.indices.refresh(index=index)
            return int(client.count(index=index)["count"]) == expected_count
        except Exception:
            return False

    def retire(self, snapshot_id: str) -> None:
        client = self.client()
        index = index_name(snapshot_id)
        if client.indices.exists(index=index):
            client.indices.delete(index=index)

    # -- provider side ---------------------------------------------------

    def _bodies(self, request: SearchRequest, scope: TrustedScope) -> tuple[dict, dict | None]:
        scope_filter = authorization_filter(scope.discovery)
        extra: list[dict] = []
        if request.object_types:
            extra.append({"terms": {"object_type": sorted(t.value for t in request.object_types)}})
        if request.table_ids:
            extra.append({"terms": {"table_id": sorted(request.table_ids)}})
        if request.domains:
            extra.append({"terms": {"domain": sorted(request.domains)}})
        if extra:
            scope_filter = {
                "bool": {"filter": [scope_filter, *extra],
                         "must_not": scope_filter["bool"]["must_not"]}
            }

        keyword = {
            "size": request.candidate_budget,
            "_source": SOURCE_FIELDS,
            "query": {
                "bool": {
                    "filter": [scope_filter],
                    "must": [{
                        "multi_match": {
                            "query": request.search_text,
                            "fields": ["title^2", "aliases^2", "content"],
                        }
                    }],
                }
            },
        }

        vector = None
        if self._embedder is not None:
            query_vector = self._embedder.embed_query(request.search_text)
            vector = {
                "size": request.candidate_budget,
                "_source": SOURCE_FIELDS,
                "query": {
                    "knn": {
                        "embedding": {
                            "vector": query_vector,
                            "k": request.candidate_budget,
                            # Filter INSIDE the knn clause, not as a post_filter.
                            "filter": scope_filter,
                        }
                    }
                },
            }
        return keyword, vector

    async def search(self, request: SearchRequest, scope: TrustedScope) -> SearchResponse:
        # The request must search the snapshot the trusted scope pins. Anything else would
        # let a caller read a catalog version their authorization was not derived against.
        if request.snapshot_id != scope.snapshot_id:
            raise RetrievalError(
                ReasonCode.SNAPSHOT_NOT_READY,
                f"Request pins snapshot {request.snapshot_id!r} but the trusted scope pins "
                f"{scope.snapshot_id!r}",
            )
        if scope.discovery.is_empty():
            return SearchResponse(
                candidates=(),
                diagnostics=ProviderDiagnostics(
                    provider=self.name, status=CompletionStatus.COMPLETE
                ),
            )

        client = self.client()
        index = index_name(request.snapshot_id)
        try:
            if not client.indices.exists(index=index):
                raise RetrievalError(
                    ReasonCode.SNAPSHOT_NOT_READY,
                    f"Index {index} does not exist",
                )
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError(
                ReasonCode.PROVIDER_UNAVAILABLE, f"OpenSearch unavailable: {exc}"
            ) from exc

        keyword_body, vector_body = self._bodies(request, scope)
        status = CompletionStatus.COMPLETE
        shard_failures = 0
        took = 0
        docs: dict[str, dict] = {}
        rankings: list[list[str]] = []

        for body in [b for b in (keyword_body, vector_body) if b is not None]:
            try:
                response = client.search(index=index, body=body)
            except Exception as exc:
                raise RetrievalError(
                    ReasonCode.PROVIDER_UNAVAILABLE, f"Search failed: {exc}"
                ) from exc

            took = max(took, int(response.get("took", 0)))
            if response.get("timed_out"):
                status = CompletionStatus.TIMED_OUT
            failed = int(response.get("_shards", {}).get("failed", 0))
            if failed:
                shard_failures += failed
                if status is CompletionStatus.COMPLETE:
                    status = CompletionStatus.PARTIAL

            ranking: list[str] = []
            for hit in response["hits"]["hits"]:
                source = hit["_source"]
                docs[source["chunk_id"]] = source
                ranking.append(source["chunk_id"])
            rankings.append(ranking)

        fused = reciprocal_rank_fusion(rankings) if rankings else []
        candidates = tuple(
            Candidate(
                chunk_id=chunk_id,
                object_id=docs[chunk_id]["object_id"],
                object_type=docs[chunk_id]["object_type"],
                table_id=docs[chunk_id].get("table_id"),
                source_version=docs[chunk_id]["source_version"],
                snapshot_id=docs[chunk_id]["snapshot_id"],
                title=docs[chunk_id]["title"],
                excerpt=docs[chunk_id]["content"][:600],
                source_ref=docs[chunk_id]["source_ref"],
                rank=i,
            )
            for i, chunk_id in enumerate(fused[: request.candidate_budget], start=1)
        )
        return SearchResponse(
            candidates=candidates,
            diagnostics=ProviderDiagnostics(
                provider=self.name,
                status=status,
                took_ms=took,
                keyword_hits=len(rankings[0]) if rankings else 0,
                vector_hits=len(rankings[1]) if len(rankings) > 1 else 0,
                shard_failures=shard_failures,
            ),
        )
