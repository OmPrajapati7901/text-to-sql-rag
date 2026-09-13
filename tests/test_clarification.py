"""Clarification and bounded repair."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.contracts.clarification import (
    ClarificationOption,
    ClarificationRequest,
    validate_answer,
)
from app.contracts.errors import Finding, GovernedError, ReasonCode
from app.contracts.retrieval import Candidate
from app.contracts.semantic_plan import Operation, SemanticRequest, SortKey, TimeRange
from app.planning import clarify as clarify_rules
from app.planning.repair import guard, propose_repair
from app.results.render import AnswerEnvelope

pytestmark = pytest.mark.unit

Q2 = TimeRange(
    start=datetime(2026, 4, 1, tzinfo=UTC), end_exclusive=datetime(2026, 7, 1, tzinfo=UTC)
)


def metric_candidates(*object_ids: str) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            chunk_id=f"c{i}", object_id=o, object_type="metric", source_version="1",
            snapshot_id="local-001", title=o, excerpt="", source_ref=f"catalog:{o}", rank=i + 1,
        )
        for i, o in enumerate(object_ids)
    )


# --- when to ask ------------------------------------------------------------


def test_exact_match_is_not_ambiguous(services, scope):
    """An official definition removes the need to ask what Revenue means."""
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2
    )
    assert clarify_rules.detect(
        services.catalog, request, metric_candidates("finance.revenue", "commerce.order_count"),
        scope, exact_metric_refs=("finance.revenue@7",),
    ) is None


def test_two_runnable_metrics_are_ambiguous(services, scope):
    request = SemanticRequest(operation=Operation.AGGREGATE, metric_ids=(), time_range=Q2)
    question = clarify_rules.detect(
        services.catalog, request,
        metric_candidates("finance.revenue", "commerce.order_count"), scope,
    )
    assert question is not None
    assert question.slot == "metric"
    assert {o.value for o in question.options} == {"finance.revenue", "commerce.order_count"}


def test_clarification_never_offers_an_unrunnable_metric(services, restricted_scope):
    """analyst_restricted may not execute Revenue, so offering it would both fail later and
    disclose that it exists."""
    request = SemanticRequest(operation=Operation.AGGREGATE, metric_ids=(), time_range=Q2)
    question = clarify_rules.detect(
        services.catalog, request,
        metric_candidates("finance.revenue", "commerce.order_count"), restricted_scope,
    )
    assert question is None, "only one metric is runnable, so there is nothing to ask"


def test_missing_period_asks(services, scope):
    request = SemanticRequest(operation=Operation.AGGREGATE, metric_ids=("finance.revenue",))
    question = clarify_rules.detect(
        services.catalog, request, (), scope, exact_metric_refs=("finance.revenue@7",)
    )
    assert question is not None
    assert question.slot == "time_range"


def test_unsupported_grain_asks_instead_of_substituting(services, scope):
    """Silently coarsening week to month would change the answer's shape without saying so."""
    request = SemanticRequest(
        operation=Operation.TREND, metric_ids=("finance.revenue",),
        time_range=Q2, time_grain="week",
    )
    question = clarify_rules.detect(
        services.catalog, request, (), scope, exact_metric_refs=("finance.revenue@7",)
    )
    assert question is not None
    assert question.slot == "time_grain"
    assert {o.value for o in question.options} == {"day", "month", "quarter", "year"}


def test_a_settled_slot_is_not_asked_twice(services, scope):
    request = SemanticRequest(operation=Operation.AGGREGATE, metric_ids=("finance.revenue",))
    assert clarify_rules.detect(
        services.catalog, request, (), scope,
        exact_metric_refs=("finance.revenue@7",),
        already_asked=frozenset({"time_range"}),
    ) is None


# --- answer validation ------------------------------------------------------


@pytest.fixture
def question() -> ClarificationRequest:
    return ClarificationRequest(
        slot="metric",
        question="Which measure?",
        options=(
            ClarificationOption(value="finance.revenue", label="Revenue"),
            ClarificationOption(value="commerce.order_count", label="Order count"),
        ),
    )


def test_answer_accepted_by_index_label_or_object(question):
    assert validate_answer("2", question).value == "commerce.order_count"
    assert validate_answer("Revenue", question).value == "finance.revenue"
    assert validate_answer(
        {"slot": "metric", "value": "finance.revenue"}, question
    ).value == "finance.revenue"


def test_resume_cannot_inject_an_unoffered_object(question):
    """A resume payload is untrusted. Without this check it would be a path straight past
    retrieval scoping and authorization."""
    with pytest.raises(GovernedError, match="NOT_AUTHORIZED"):
        validate_answer({"slot": "metric", "value": "finance.secret"}, question)


def test_resume_cannot_answer_a_different_slot(question):
    with pytest.raises(GovernedError, match="MATERIAL_AMBIGUITY"):
        validate_answer({"slot": "time_grain", "value": "finance.revenue"}, question)


def test_unmatched_freeform_is_rejected(question):
    with pytest.raises(GovernedError, match="MATERIAL_AMBIGUITY"):
        validate_answer("whatever you think", question)


# --- repair -----------------------------------------------------------------


def test_repair_drops_a_sort_key_naming_no_output_column():
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2,
        order_by=(SortKey(field="revenue_usd"), SortKey(field="not_a_column")),
    )
    outcome = propose_repair(
        request, [Finding(code=ReasonCode.VALIDATION_FAILED, message="x")],
        result_columns=("revenue_usd",),
    )
    assert outcome.repaired
    assert [k.field for k in outcome.request.order_by] == ["revenue_usd"]


@pytest.mark.parametrize("code", [
    ReasonCode.NOT_AUTHORIZED,
    ReasonCode.MANDATORY_RULE_UNRESOLVED,
    ReasonCode.NO_APPROVED_PATH,
    ReasonCode.FAN_OUT_UNPROVEN,
    ReasonCode.UNSUPPORTED_CAPABILITY,
])
def test_semantic_failures_are_never_repaired(code):
    """A denied operation or missing approved relationship is not an invitation to change
    the question."""
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2
    )
    outcome = propose_repair(request, [Finding(code=code, message="x")])
    assert not outcome.repaired
    assert outcome.request is None


def test_guard_blocks_every_meaning_changing_diff():
    base = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2
    )
    wider = TimeRange(
        start=datetime(2020, 1, 1, tzinfo=UTC), end_exclusive=datetime(2030, 1, 1, tzinfo=UTC)
    )
    for field, mutated in [
        ("metric_ids", base.model_copy(update={"metric_ids": ("commerce.order_count",)})),
        ("time_range", base.model_copy(update={"time_range": wider})),
        ("limit", base.model_copy(update={"limit": 9999})),
    ]:
        with pytest.raises(GovernedError, match="VALIDATION_FAILED"):
            guard(base, mutated)
        assert field  # the field under test is named for failure output


# --- the repair loop, end to end -------------------------------------------


@pytest.mark.e2e
async def test_repair_loop_recovers_and_preserves_the_numbers(services):
    """A proposal sorts by a column it never projected. Repair drops the sort key, the plan
    is re-bound and re-validated from scratch, and the measure is unchanged."""
    from app.graph.nodes import core
    from app.runtime import AppRuntime

    runtime = AppRuntime.build(use_llm=False, use_embeddings=False)
    ctx = runtime.context("analyst_full")

    class _Runtime:
        context = ctx

    proposal = SemanticRequest(
        operation=Operation.AGGREGATE,
        metric_ids=("finance.revenue",),
        dimension_ids=("customer.region_at_order",),
        time_range=Q2,
        order_by=(SortKey(field="revenue_usd"), SortKey(field="profit_margin")),
    )
    state = {
        "request_id": "t_repair", "thread_id": "t_repair", "question": "revenue by region",
        "proposal": proposal.model_dump(mode="json"), "plan_revision": 1,
        "repair_attempts": 0, "retrieval_rounds": 1, "clarified_slots": [],
        "notes": [], "reason_codes": [], "candidates": [],
        "exact_metric_refs": ["finance.revenue@7"],
    }

    first = await core.bind_validate_compile(state, _Runtime())
    assert first["decision"] == "repairable"

    state = {**state, **first}
    fixed = await core.repair(state, _Runtime())
    assert fixed["decision"] == "ready"
    assert fixed["repair_attempts"] == 1
    assert fixed["plan_revision"] == 2, "a repaired plan gets a new revision"
    assert fixed["sql"] is None, "downstream artifacts must be cleared by explicit overwrite"

    state = {**state, **fixed}
    second = await core.bind_validate_compile(state, _Runtime())
    assert second["decision"] == "ready", "a repaired plan passes every gate again"

    state = {**state, **second}
    done = await core.execute(state, _Runtime())
    assert done["decision"] == "answered"

    env = AnswerEnvelope.model_validate(done["answer"])
    totals = {row[0]: str(row[1]) for row in env.rows}
    assert totals == {"North": "200.00", "South": "50.00"}, (
        "repair must not change the measure"
    )


@pytest.mark.e2e
async def test_repair_budget_is_enforced(services):
    from app.graph.nodes import core
    from app.runtime import AppRuntime

    runtime = AppRuntime.build(use_llm=False, use_embeddings=False)

    class _Runtime:
        context = runtime.context("analyst_full")

    proposal = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2
    )
    exhausted = {
        "request_id": "t_budget", "proposal": proposal.model_dump(mode="json"),
        "plan_revision": 3, "repair_attempts": 2, "notes": ["x"],
        "reason_codes": ["VALIDATION_FAILED"],
    }
    out = await core.repair(exhausted, _Runtime())
    assert out["decision"] == "blocked"
    assert "BUDGET_EXHAUSTED" in out["reason_codes"]
