"""Retrieval orchestration.

Exact authorized matches resolve straightforward requests in code before any search runs — a
glossary hit on "top line" settles the metric without a model call or a vector lookup.

Search is a discovery aid. Candidates are re-authorized after ranking, and mandatory metric,
rule and join dependencies are resolved by the catalog regardless of whether they ranked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.catalog.service import CatalogService
from app.contracts.errors import Finding, ReasonCode, RetrievalError, Severity
from app.contracts.ids import ObjectType
from app.contracts.retrieval import Candidate, CompletionStatus, SearchRequest
from app.contracts.scope import Action, TrustedScope
from app.retrieval.fusion import cap_per_source
from app.retrieval.providers.base import RetrievalProvider

MAX_PER_SOURCE = 3


@dataclass
class RetrievalOutcome:
    candidates: tuple[Candidate, ...]
    exact_matches: tuple[str, ...]
    findings: tuple[Finding, ...] = ()
    provider: str = ""
    status: CompletionStatus = CompletionStatus.COMPLETE

    @property
    def is_complete(self) -> bool:
        return self.status is CompletionStatus.COMPLETE


@dataclass
class RetrievalService:
    catalog: CatalogService
    provider: RetrievalProvider
    _rounds: int = field(default=0, repr=False)

    def resolve_exact(self, text: str, scope: TrustedScope) -> list[str]:
        """Exact glossary/alias resolution, in code. No search, no model."""
        hits: list[str] = []
        for entry in self.catalog.lookup_term(text, scope):
            hits.extend(entry.maps_to)

        needle = text.strip().lower()
        for metric in self.catalog.snapshot.all("metrics"):
            names = {metric.name.lower(), metric.id.lower(), *(a.lower() for a in metric.aliases)}
            if needle in names and self.catalog.policy.check(
                scope.principal_id, Action.DISCOVER, metric.id
            ).allowed:
                hits.append(str(metric.ref))
        for dim in self.catalog.snapshot.all("dimensions"):
            names = {dim.name.lower(), dim.id.lower(), *(a.lower() for a in dim.aliases)}
            if needle in names and self.catalog.policy.check(
                scope.principal_id, Action.DISCOVER, dim.id
            ).allowed:
                hits.append(str(dim.ref))
        return sorted(set(hits))

    async def search(
        self,
        text: str,
        scope: TrustedScope,
        *,
        object_types: frozenset[ObjectType] = frozenset(),
        table_ids: frozenset[str] = frozenset(),
        budget: int = 40,
    ) -> RetrievalOutcome:
        request = SearchRequest(
            search_text=text,
            object_types=object_types,
            table_ids=table_ids,
            candidate_budget=budget,
            snapshot_id=scope.snapshot_id,
        )
        try:
            response = await self.provider.search(request, scope)
        except RetrievalError as exc:
            return RetrievalOutcome(
                candidates=(), exact_matches=(),
                findings=(exc.finding,),
                provider=getattr(self.provider, "name", "unknown"),
                status=CompletionStatus.FAILED,
            )

        findings: list[Finding] = list(response.diagnostics.findings)
        if not response.is_complete:
            # Service incompleteness is distinct from semantic incompleteness, and is never
            # presented as a short but complete candidate list.
            findings.append(Finding(
                code=ReasonCode.PARTIAL_RETRIEVAL,
                severity=Severity.WARN,
                message=f"Provider reported {response.diagnostics.status.value} "
                        f"({response.diagnostics.shard_failures} shard failure(s)); the "
                        f"candidate list may be incomplete.",
            ))

        # Re-authorize after ranking. A candidate's discovery permission is rechecked before
        # it can reach a reranker or a model prompt.
        authorized = tuple(
            c for c in response.candidates
            if self.catalog.policy.check(scope.principal_id, Action.DISCOVER, c.object_id).allowed
        )
        dropped = len(response.candidates) - len(authorized)
        if dropped:
            findings.append(Finding(
                code=ReasonCode.DISCOVERY_DENIED,
                severity=Severity.INFO,
                message=f"{dropped} candidate(s) removed by a post-ranking authorization "
                        f"recheck",
            ))

        # Stop one verbose table's columns from crowding out other required objects.
        source_of = {c.chunk_id: (c.table_id or c.object_id) for c in authorized}
        keep = set(cap_per_source([c.chunk_id for c in authorized], source_of, MAX_PER_SOURCE))
        capped = tuple(
            c.model_copy(update={"rank": i})
            for i, c in enumerate((c for c in authorized if c.chunk_id in keep), start=1)
        )

        return RetrievalOutcome(
            candidates=capped,
            exact_matches=tuple(self.resolve_exact(text, scope)),
            findings=tuple(findings),
            provider=response.diagnostics.provider,
            status=response.diagnostics.status,
        )

    async def columns_for_table(
        self, table_id: str, text: str, scope: TrustedScope, budget: int = 15
    ) -> tuple[Candidate, ...]:
        """Table-scoped column search for wide tables. Exact dependency access is still
        available through the catalog; this only prioritizes descriptions."""
        outcome = await self.search(
            text, scope,
            object_types=frozenset({ObjectType.COLUMN}),
            table_ids=frozenset({table_id}),
            budget=budget,
        )
        return outcome.candidates
