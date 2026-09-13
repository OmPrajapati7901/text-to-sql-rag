"""Material-ambiguity detection.

Clarify only when alternatives would change the answer and no authoritative default settles
them (DESIGN §21). Two rules keep this from becoming a nag:

* An exact glossary or alias match is not ambiguous. "Revenue" binds to the official metric;
  asking what Revenue means would be noise.
* Differences between equivalent physical access paths never reach the user. Only business
  meaning does.

Choose the smallest question that separates the largest number of consequential alternatives,
and never offer an object the principal cannot already discover.
"""

from __future__ import annotations

from app.catalog.service import CatalogService
from app.contracts.clarification import ClarificationOption, ClarificationRequest
from app.contracts.retrieval import Candidate
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantic_plan import SemanticRequest
from app.contracts.semantics import TimeGrain


def _executable_metrics(
    catalog: CatalogService, object_ids: list[str], scope: TrustedScope
) -> list[str]:
    """Only metrics the principal may actually run. A clarification must never reveal that a
    metric exists if discovery policy would not."""
    out = []
    for object_id in object_ids:
        if not catalog.policy.check(scope.principal_id, Action.DISCOVER, object_id).allowed:
            continue
        if not catalog.policy.check(
            scope.principal_id, Action.EXECUTE_METRIC, object_id
        ).allowed:
            continue
        out.append(object_id)
    return out


def ambiguous_measure(
    catalog: CatalogService,
    candidates: tuple[Candidate, ...],
    scope: TrustedScope,
    *,
    exact_metric_refs: tuple[str, ...] = (),
) -> ClarificationRequest | None:
    """Several authorized metrics could answer this, and no exact match settles it."""
    if exact_metric_refs:
        return None  # an exact alias match is authoritative

    metric_ids = [c.object_id for c in candidates if c.object_type == "metric"]
    runnable = _executable_metrics(catalog, list(dict.fromkeys(metric_ids)), scope)
    if len(runnable) < 2:
        return None

    options = []
    for object_id in runnable[:4]:
        record = catalog.snapshot.get("metrics", object_id)
        unit = f" ({record.unit})" if record.unit else ""
        options.append(ClarificationOption(
            value=object_id, label=f"{record.name}{unit}", effect=record.description
        ))
    return ClarificationRequest(
        slot="metric",
        question="Which measure do you mean?",
        options=tuple(options),
        reason="Several official metrics could answer this, and they give different numbers.",
    )


def unsupported_grain(
    catalog: CatalogService, request: SemanticRequest, scope: TrustedScope
) -> ClarificationRequest | None:
    """The requested time grain is not one this metric supports.

    Silently substituting a coarser grain would change the answer's shape without saying so,
    so the supported grains are offered instead.
    """
    if not request.time_grain or not request.metric_ids:
        return None
    try:
        metric = catalog.snapshot.get("metrics", request.metric_ids[0])
    except Exception:
        return None
    if request.time_grain in metric.time.grains:
        return None

    options = [
        ClarificationOption(
            value=grain.value,
            label=f"By {grain.value}",
            effect=f"One row per {grain.value} per grouping",
        )
        for grain in metric.time.grains
    ]
    if len(options) < 2:
        return None
    return ClarificationRequest(
        slot="time_grain",
        question=(
            f"{metric.name} is not published at {request.time_grain.value} grain. "
            f"Which period should it be broken down by?"
        ),
        options=tuple(options),
        reason=f"{metric.ref} supports "
               f"{', '.join(g.value for g in metric.time.grains)}.",
    )


def missing_time_range(
    catalog: CatalogService, request: SemanticRequest, scope: TrustedScope
) -> ClarificationRequest | None:
    """A metric query with no period.

    There is no approved default reporting period in this catalog, and inventing one would
    silently decide the analytical population. The timezone *does* have an approved default,
    which is why that is resolved in code and never asked about.
    """
    if request.time_range is not None or not request.metric_ids:
        return None
    return ClarificationRequest(
        slot="time_range",
        question="Which period should this cover?",
        options=(
            ClarificationOption(
                value="2026-04-01/2026-07-01", label="Q2 2026",
                effect="1 April to 30 June 2026 inclusive",
            ),
            ClarificationOption(
                value="2026-01-01/2027-01-01", label="Calendar year 2026",
                effect="1 January to 31 December 2026 inclusive",
            ),
            ClarificationOption(
                value="2026-01-01/2026-04-01", label="Q1 2026",
                effect="1 January to 31 March 2026 inclusive",
            ),
        ),
        reason="No reporting period was stated and this catalog publishes no default period.",
    )


def apply_answer(request: SemanticRequest, slot: str, value: str) -> SemanticRequest:
    """Apply a validated clarification to the proposal.

    Only the clarified slot changes. Everything else is carried forward, so answering one
    question never silently re-opens another.
    """
    from datetime import UTC, datetime

    if slot == "metric":
        return request.model_copy(update={"metric_ids": (value,)})
    if slot == "time_grain":
        return request.model_copy(update={"time_grain": TimeGrain(value)})
    if slot == "time_range":
        from app.contracts.semantic_plan import TimeRange

        start, _, end = value.partition("/")
        return request.model_copy(update={
            "time_range": TimeRange(
                start=datetime.fromisoformat(start).replace(tzinfo=UTC),
                end_exclusive=datetime.fromisoformat(end).replace(tzinfo=UTC),
            )
        })
    return request


def detect(
    catalog: CatalogService,
    request: SemanticRequest,
    candidates: tuple[Candidate, ...],
    scope: TrustedScope,
    *,
    exact_metric_refs: tuple[str, ...] = (),
    already_asked: frozenset[str] = frozenset(),
) -> ClarificationRequest | None:
    """First unresolved material ambiguity, or None.

    `already_asked` prevents re-asking a slot the user has settled: a clarification preserves
    resolved slots rather than restarting the dialogue.
    """
    # Never ask a question whose answer cannot change the outcome. If the proposed metric is
    # not executable by this principal, the request is denied either way, and asking would be
    # pure friction -- the denial should arrive on the first turn, not the second.
    for metric_id in request.metric_ids:
        if not catalog.policy.check(
            scope.principal_id, Action.EXECUTE_METRIC, metric_id
        ).allowed:
            return None

    checks = (
        ("metric", lambda: ambiguous_measure(
            catalog, candidates, scope, exact_metric_refs=exact_metric_refs
        )),
        ("time_range", lambda: missing_time_range(catalog, request, scope)),
        ("time_grain", lambda: unsupported_grain(catalog, request, scope)),
    )
    for slot, check in checks:
        if slot in already_asked:
            continue
        found = check()
        if found is not None:
            return found
    return None
