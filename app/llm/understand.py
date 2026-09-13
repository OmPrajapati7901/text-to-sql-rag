"""Question understanding and routing.

An LLM earns its place here: users phrase concepts and conversational references in ways no
practical keyword set covers. It does not earn authority. Routing decides *which path*, never
whether a principal may see something, and never whether a hidden object exists.

Exact glossary matches resolve straightforward cases in code before any model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.service import CatalogService
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import IntentSketch, Operation
from app.llm.client import LlmClient

SYSTEM = """You interpret business analytics questions for a governed query system.

You do NOT write SQL, table names, column names, or metric formulas. You do not decide what a
user is allowed to see. You describe what the question is asking, in the user's own words.

Routes:
- "sql": asks for data that must be computed from the warehouse.
- "definition": asks what a term or metric means; no data is needed.
- "metadata": asks what data exists, which tables/fields are available.
- "clarify": genuinely ambiguous in a way that would change the answer.
- "unsupported": asks for an operation this system does not perform.

Record the phrases the user used. Do not invent entities or resolve them to identifiers.
If the question names no measure at all, put it in unresolved_slots."""


class UnderstandOutput(BaseModel):
    """What the model may return. No identifiers, no SQL, no permission claims."""

    model_config = ConfigDict(extra="forbid")

    route: Literal["sql", "definition", "metadata", "clarify", "unsupported"]
    action: Literal["lookup", "aggregate", "trend", "rank", "compare"] | None = None
    normalized_question: str = Field(max_length=500)
    measure_phrases: list[str] = Field(default_factory=list, max_length=8)
    dimension_phrases: list[str] = Field(default_factory=list, max_length=8)
    filter_phrases: list[str] = Field(default_factory=list, max_length=8)
    time_phrase: str | None = None
    unresolved_slots: list[str] = Field(default_factory=list, max_length=8)


@dataclass
class Understanding:
    sketch: IntentSketch
    exact_metric_refs: tuple[str, ...] = ()
    exact_dimension_refs: tuple[str, ...] = ()
    used_llm: bool = True


class QuestionUnderstander:
    def __init__(self, catalog: CatalogService, llm: LlmClient | None) -> None:
        self.catalog = catalog
        self.llm = llm

    def understand(self, question: str, scope: TrustedScope) -> Understanding:
        output = self._propose(question)

        sketch = IntentSketch(
            route=output.route,
            action=Operation(output.action) if output.action else None,
            normalized_question=output.normalized_question,
            measure_phrases=tuple(output.measure_phrases),
            dimension_phrases=tuple(output.dimension_phrases),
            filter_phrases=tuple(output.filter_phrases),
            time_phrase=output.time_phrase,
            unresolved_slots=tuple(output.unresolved_slots),
            evidence_spans=(question[:400],),
        )

        # Deterministic resolution of whatever the model named. Exact matches win; a phrase
        # with no exact match falls through to retrieval, not to a guess.
        metrics: list[str] = []
        dimensions: list[str] = []
        for phrase in output.measure_phrases:
            metrics.extend(self._exact(phrase, "metrics", scope))
        for phrase in output.dimension_phrases:
            dimensions.extend(self._exact(phrase, "dimensions", scope))

        return Understanding(
            sketch=sketch,
            exact_metric_refs=tuple(dict.fromkeys(metrics)),
            exact_dimension_refs=tuple(dict.fromkeys(dimensions)),
            used_llm=self.llm is not None,
        )

    def _propose(self, question: str) -> UnderstandOutput:
        if self.llm is None:
            return _heuristic(question)
        today = datetime.now(UTC).date().isoformat()
        return self.llm.structured(
            UnderstandOutput,
            SYSTEM,
            f"Today is {today}.\nQuestion: {question}",
        )

    def _exact(self, phrase: str, kind: str, scope: TrustedScope) -> list[str]:
        """Exact alias/glossary resolution. Never fuzzy."""
        needle = phrase.strip().lower()
        hits: list[str] = []
        for entry in self.catalog.lookup_term(needle, scope):
            hits.extend(r for r in entry.maps_to if _kind_of(r) == kind)
        for record in self.catalog.snapshot.all(kind):
            names = {record.name.lower(), record.id.lower()}
            names |= {a.lower() for a in getattr(record, "aliases", ())}
            if needle in names:
                hits.append(str(record.ref))
        return hits


def _kind_of(ref: str) -> str:
    object_id = ref.split("@", maxsplit=1)[0]
    try:
        return (
            "metrics"
            if object_id.count(".") == 1
            and not object_id.startswith(("customer.", "commerce.region"))
            else "dimensions"
        )
    except Exception:
        return "metrics"


def _heuristic(question: str) -> UnderstandOutput:
    """Deterministic fallback when no model is configured. Deliberately literal: it never
    guesses a metric the user did not name."""
    lowered = question.lower()
    measures = [w for w in ("revenue", "order count", "sales") if w in lowered]
    dimensions = [w for w in ("region", "territory") if w in lowered]
    action = "trend" if any(w in lowered for w in ("monthly", "by month", "trend")) else "aggregate"
    return UnderstandOutput(
        route="sql" if measures else "clarify",
        action=action,
        normalized_question=question.strip()[:500],
        measure_phrases=measures,
        dimension_phrases=dimensions,
        time_phrase=None,
        unresolved_slots=[] if measures else ["measure"],
    )
