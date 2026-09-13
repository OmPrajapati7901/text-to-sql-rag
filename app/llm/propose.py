"""Semantic plan proposal.

The model receives authorized candidate IDs, concise business descriptions and the supported
operator set. It returns IDs and operators only. It cannot write SQL, invent an identifier, or
select an object that was not offered to it — the proposal is validated against the offered
set here, and every reference is bound again by the binder afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.service import CatalogService
from app.contracts.errors import GovernedError, ReasonCode
from app.contracts.retrieval import Candidate
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import (
    Operation,
    SemanticRequest,
    TimeRange,
)
from app.llm.client import LlmClient

SYSTEM = """You map a business question onto a governed analytics request.

Hard rules:
- Use ONLY the metric_id and dimension_id values listed in the candidates below. If the
  metric you need is not listed, return an empty metric_ids list and say so in notes.
- You may not write SQL, table names, column names, join conditions, or formulas.
- Time ranges are half-open: start is inclusive, end_exclusive is exclusive. "Q2 2026" is
  2026-04-01T00:00:00Z to 2026-07-01T00:00:00Z.
- Choose operation "trend" only when the user asked for a breakdown over time.
- Do not invent a metric because it sounds plausible. An empty answer is correct when the
  catalog does not offer what was asked for."""


class TimeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(description="ISO-8601 instant, inclusive")
    end_exclusive: str = Field(description="ISO-8601 instant, exclusive")


class ProposalOutput(BaseModel):
    """The model's proposal. Every field is an ID or an enumerated operator."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["lookup", "aggregate", "trend", "rank", "compare"]
    metric_ids: list[str] = Field(default_factory=list, max_length=3)
    dimension_ids: list[str] = Field(default_factory=list, max_length=3)
    time: TimeProposal | None = None
    time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)
    notes: str = Field(default="", max_length=400)


@dataclass
class Proposal:
    request: SemanticRequest | None
    notes: str
    rejected: tuple[str, ...] = ()


class PlanProposer:
    def __init__(self, catalog: CatalogService, llm: LlmClient | None) -> None:
        self.catalog = catalog
        self.llm = llm

    def propose(
        self,
        question: str,
        candidates: tuple[Candidate, ...],
        scope: TrustedScope,
        *,
        exact_metric_refs: tuple[str, ...] = (),
        exact_dimension_refs: tuple[str, ...] = (),
    ) -> Proposal:
        offered_metrics, offered_dimensions = self._offered(
            candidates, exact_metric_refs, exact_dimension_refs
        )
        if not offered_metrics:
            return Proposal(
                request=None,
                notes="No authorized metric matched this question.",
            )
        if self.llm is None:
            return self._without_model(question, offered_metrics, offered_dimensions)

        output = self.llm.structured(
            ProposalOutput, SYSTEM, self._prompt(question, offered_metrics, offered_dimensions)
        )

        # The model may only choose from what it was offered. Anything else is discarded --
        # not repaired, not fuzzy-matched.
        rejected: list[str] = []
        metric_ids = []
        for mid in output.metric_ids:
            if mid in offered_metrics:
                metric_ids.append(mid)
            else:
                rejected.append(f"metric {mid!r} was not offered")
        dimension_ids = []
        for did in output.dimension_ids:
            if did in offered_dimensions:
                dimension_ids.append(did)
            else:
                rejected.append(f"dimension {did!r} was not offered")

        if not metric_ids:
            return Proposal(
                request=None,
                notes=output.notes or "The proposal named no authorized metric.",
                rejected=tuple(rejected),
            )

        time_range = None
        if output.time:
            try:
                time_range = TimeRange(
                    start=_parse_instant(output.time.start),
                    end_exclusive=_parse_instant(output.time.end_exclusive),
                )
            except (ValueError, TypeError) as exc:
                raise GovernedError(
                    ReasonCode.MATERIAL_AMBIGUITY,
                    f"Proposed time range could not be resolved: {exc}",
                ) from exc

        return Proposal(
            request=SemanticRequest(
                operation=Operation(output.operation),
                metric_ids=tuple(metric_ids),
                dimension_ids=tuple(dimension_ids),
                filters=(),
                time_range=time_range,
                time_grain=output.time_grain,
                limit=output.limit,
            ),
            notes=output.notes,
            rejected=tuple(rejected),
        )

    def _offered(
        self,
        candidates: tuple[Candidate, ...],
        exact_metric_refs: tuple[str, ...],
        exact_dimension_refs: tuple[str, ...],
    ) -> tuple[dict[str, str], dict[str, str]]:
        metrics: dict[str, str] = {}
        dimensions: dict[str, str] = {}
        for ref in exact_metric_refs:
            object_id = ref.split("@")[0]
            record = self.catalog.snapshot.get("metrics", object_id)
            metrics[object_id] = record.description
        for ref in exact_dimension_refs:
            object_id = ref.split("@")[0]
            record = self.catalog.snapshot.get("dimensions", object_id)
            dimensions[object_id] = record.description
        for candidate in candidates:
            if candidate.object_type == "metric":
                metrics.setdefault(candidate.object_id, candidate.excerpt[:200])
            elif candidate.object_type == "dimension":
                dimensions.setdefault(candidate.object_id, candidate.excerpt[:200])
        return metrics, dimensions

    def _prompt(self, question: str, metrics: dict[str, str], dimensions: dict[str, str]) -> str:
        lines = [f"Today is {datetime.now(UTC).date().isoformat()}.", "", "Available metrics:"]
        lines += [f"  - {k}: {v}" for k, v in metrics.items()]
        lines += ["", "Available dimensions:"]
        lines += [f"  - {k}: {v}" for k, v in dimensions.items()] or ["  (none)"]
        lines += ["", f"Question: {question}"]
        return "\n".join(lines)

    def _without_model(
        self,
        question: str,
        metrics: dict[str, str],
        dimensions: dict[str, str],
    ) -> Proposal:
        lowered = question.lower()
        monthly = "monthly" in lowered or "by month" in lowered
        return Proposal(
            request=SemanticRequest(
                operation=Operation.TREND if monthly else Operation.AGGREGATE,
                metric_ids=(next(iter(metrics)),),
                dimension_ids=tuple(dimensions)[:1],
                time_range=_literal_quarter(question),
                time_grain="month" if monthly else None,
            ),
            notes="Deterministic fallback: no language model configured.",
        )


def _parse_instant(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _literal_quarter(question: str) -> TimeRange | None:
    """Parse only an explicitly stated calendar quarter; never infer a missing period."""

    match = re.search(r"\bq([1-4])\s+(\d{4})\b", question, flags=re.IGNORECASE)
    if match is None:
        return None
    quarter = int(match.group(1))
    year = int(match.group(2))
    start_month = (quarter - 1) * 3 + 1
    end_year = year + 1 if start_month == 10 else year
    end_month = 1 if start_month == 10 else start_month + 3
    return TimeRange(
        start=datetime(year, start_month, 1, tzinfo=UTC),
        end_exclusive=datetime(end_year, end_month, 1, tzinfo=UTC),
    )
