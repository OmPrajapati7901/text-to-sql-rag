"""Provider contract suite.

Every test here runs against every provider. Equivalent *supported behaviour* is required;
identical ranks and raw scores are not.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from app.contracts.ids import ObjectType
from app.contracts.retrieval import SearchRequest
from app.retrieval.embeddings import EmbeddingClient
from app.retrieval.providers.inmemory import InMemoryProvider
from app.retrieval.providers.opensearch import OpenSearchProvider

load_dotenv()

pytestmark = pytest.mark.contract

AMOUNT = "warehouse.commerce.orders.order_amount_usd"


def _opensearch_up() -> bool:
    try:
        import httpx

        url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
        return httpx.get(url, timeout=2.0).status_code == 200
    except Exception:
        return False


def _embedder_up() -> bool:
    try:
        return EmbeddingClient().health()
    except Exception:
        return False


OPENSEARCH_AVAILABLE = _opensearch_up()
EMBEDDER_AVAILABLE = _embedder_up()


@pytest.fixture(scope="module")
def cards(services):
    from ingestion.export import embed_cards, export_cards

    exported = export_cards(services.catalog.snapshot)
    if EMBEDDER_AVAILABLE:
        exported = embed_cards(exported, EmbeddingClient())
    return exported


@pytest.fixture(params=["inmemory", "opensearch"])
def provider(request, cards):
    """Each contract test runs against both implementations."""
    if request.param == "opensearch":
        if not OPENSEARCH_AVAILABLE:
            pytest.skip("OpenSearch not reachable at OPENSEARCH_URL")
        p = OpenSearchProvider()
        p.retire("local-001")
        p.publish([c.to_document() for c in cards], "local-001")
        if EMBEDDER_AVAILABLE:
            p.set_embedder(EmbeddingClient())
        yield p
        p.retire("local-001")
    else:
        p = InMemoryProvider()
        p.index([c.to_document() for c in cards])
        if EMBEDDER_AVAILABLE:
            p.set_embedder(EmbeddingClient())
        yield p


def req(text: str, **kw) -> SearchRequest:
    kw.setdefault("snapshot_id", "local-001")
    return SearchRequest(search_text=text, **kw)


async def test_finds_the_official_metric(provider, scope):
    r = await provider.search(req("total revenue", candidate_budget=20), scope)
    assert "finance.revenue" in {c.object_id for c in r.candidates}


async def test_reports_completion_status(provider, scope):
    r = await provider.search(req("revenue"), scope)
    assert r.diagnostics.status.value in {"complete", "partial", "timed_out"}
    assert r.diagnostics.provider


async def test_denied_domain_is_filtered_inside_the_search_boundary(provider, restricted_scope):
    """A restricted principal must never see finance objects in candidates -- not even to
    have them reranked away later."""
    request = req("invoices billing payments", candidate_budget=40)
    r = await provider.search(request, restricted_scope)
    assert not any(c.object_id.startswith("warehouse.finance") for c in r.candidates)


async def test_denied_object_is_filtered(provider, restricted_scope):
    r = await provider.search(req("order amount usd value", candidate_budget=40), restricted_scope)
    assert not any(c.object_id == AMOUNT for c in r.candidates)


async def test_full_principal_still_sees_those_objects(provider, scope):
    """Confirms the previous two tests filter by policy, not by a broken query."""
    r = await provider.search(req("order amount usd value", candidate_budget=40), scope)
    assert AMOUNT in {c.object_id for c in r.candidates}


async def test_object_type_filter(provider, scope):
    r = await provider.search(
        req("revenue", object_types=frozenset({ObjectType.METRIC}), candidate_budget=20), scope
    )
    assert r.candidates
    assert all(c.object_type == "metric" for c in r.candidates)


async def test_table_scoped_column_search(provider, scope):
    """Requires the parent table_id the sample sidecars in DESIGN/ omit."""
    r = await provider.search(
        req(
            "region",
            object_types=frozenset({ObjectType.COLUMN}),
            table_ids=frozenset({"warehouse.commerce.customer_history"}),
            candidate_budget=20,
        ),
        scope,
    )
    assert r.candidates
    assert all(c.table_id == "warehouse.commerce.customer_history" for c in r.candidates)


async def test_unknown_snapshot_fails_closed(provider, services):
    """A snapshot the provider has not indexed must raise, never return unscoped results."""
    from app.authorization.scope import establish_scope
    from app.contracts.errors import RetrievalError

    bogus = establish_scope(services.policy, "analyst_full", "no-such-snap")
    with pytest.raises(RetrievalError):
        await provider.search(req("revenue", snapshot_id="no-such-snap"), bogus)


async def test_request_snapshot_must_match_scope_snapshot(provider, services):
    """A caller must not be able to search a snapshot its scope was not derived against."""
    from app.authorization.scope import establish_scope
    from app.contracts.errors import RetrievalError

    bogus = establish_scope(services.policy, "analyst_full", "no-such-snap")
    with pytest.raises(RetrievalError, match="SNAPSHOT_NOT_READY"):
        await provider.search(req("revenue", snapshot_id="local-001"), bogus)


async def test_candidates_carry_provenance(provider, scope):
    r = await provider.search(req("revenue", candidate_budget=5), scope)
    for c in r.candidates:
        assert c.source_ref.startswith("catalog:")
        assert c.snapshot_id == "local-001"
        assert c.rank >= 1


async def test_capabilities_are_declared(provider):
    caps = provider.capabilities()
    assert caps.name
    assert caps.supports_domain_filter
    assert caps.supports_table_scoped_columns
