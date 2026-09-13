"""End-to-end pipeline tests against DuckDB fixtures.

The golden case is the §40A worked example from the architecture document, so its expected
output was specified and reviewed before this code existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.contracts.semantic_plan import Operation, SemanticRequest, TimeRange

pytestmark = pytest.mark.e2e

Q2 = TimeRange(
    start=datetime(2026, 4, 1, tzinfo=UTC), end_exclusive=datetime(2026, 7, 1, tzinfo=UTC)
)


def rows_of(outcome):
    return [
        (r[0].date().isoformat(), r[1], str(r[2])) for r in outcome.answer.rows
    ]


# --- the golden case --------------------------------------------------------


def test_golden_matches_the_design_document(pipeline, scope, revenue_by_region):
    """DESIGN §40A: April/North $200.00 and June/South $50.00, and nothing else."""
    outcome = pipeline("golden").run(revenue_by_region, scope)
    assert outcome.answer.status == "answered", outcome.answer.notes
    assert rows_of(outcome) == [
        ("2026-04-01", "North", "200.00"),
        ("2026-06-01", "South", "50.00"),
    ]


def test_golden_reports_its_provenance(pipeline, scope, revenue_by_region):
    outcome = pipeline("golden").run(revenue_by_region, scope)
    answer = outcome.answer
    assert answer.metric_versions == {"finance.revenue": "v7"}
    assert "global.exclude_internal_customers@2" in answer.applied_rules
    assert "security.tenant_scope@5" in answer.applied_rules
    assert answer.join_paths == ("commerce.orders_customers_asof@2",)
    assert answer.unit == "USD"
    assert "end exclusive" in answer.time_interpretation


def test_excluded_rows_are_genuinely_excluded(pipeline, scope, revenue_by_region):
    """Each excluded row has a distinct reason; any one leaking changes the total."""
    outcome = pipeline("golden").run(revenue_by_region, scope)
    total = sum(Decimal(r[2]) for r in rows_of(outcome))
    assert total == Decimal("250.00"), (
        "Total must exclude the internal customer (999), test customer (888), test order "
        "(777), fully refunded order (666), pending order (555) and out-of-range order (444)."
    )


def test_other_tenant_rows_are_invisible(pipeline, scope, revenue_by_region):
    """tenant-other has an order with the same order_id and a 5000 amount."""
    outcome = pipeline("golden").run(revenue_by_region, scope)
    total = sum(Decimal(r[2]) for r in rows_of(outcome))
    assert total == Decimal("250.00")
    assert "5000" not in str(outcome.answer.rows)


def test_cross_tenant_query_sees_only_its_own_rows(pipeline, other_tenant_scope, revenue_by_region):
    """The same request under a different tenant must return that tenant's data only."""
    outcome = pipeline("golden").run(revenue_by_region, other_tenant_scope)
    assert outcome.answer.status == "answered", outcome.answer.notes
    assert rows_of(outcome) == [("2026-04-01", "North", "5000.00")]


# --- counterexample fixtures ------------------------------------------------


def test_partial_refund_amount_is_retained(pipeline, scope, revenue_by_region):
    """Revenue v7 keeps the original amount for a partially refunded order. Excluding it
    would under-report by 200."""
    outcome = pipeline("partial_refund").run(revenue_by_region, scope)
    total = sum(Decimal(r[2]) for r in rows_of(outcome))
    assert total == Decimal("450.00"), "the 200.00 partially refunded order must be included"


def test_equal_amounts_are_not_collapsed(pipeline, scope):
    """Two distinct orders of 100.00 each. SUM(DISTINCT) would report 100, not 200 --
    which is why DISTINCT is never a fan-out repair."""
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",),
        dimension_ids=("customer.region_at_order",), time_range=Q2,
    )
    outcome = pipeline("equal_amounts").run(request, scope)
    assert outcome.answer.status == "answered", outcome.answer.notes
    assert Decimal(str(outcome.answer.rows[0][1])) == Decimal("200.00")


def test_empty_result_is_reported_honestly(pipeline, scope, revenue_by_region):
    """A legitimate empty result is not a failure, and filters are never loosened."""
    outcome = pipeline("empty").run(revenue_by_region, scope)
    assert outcome.answer.status == "answered"
    assert outcome.answer.rows == ()
    assert any("not evidence that the underlying data is absent" in n
               for n in outcome.answer.notes)


def test_scd_overlap_blocks_on_the_uniqueness_attestation(pipeline, scope, revenue_by_region):
    """Overlapping customer versions double count the order across two regions while the
    output grain stays perfectly unique -- so only the pre-execution attestation can catch
    it. An inner join and EXISTS both report success here."""
    outcome = pipeline("scd_overlap").run(revenue_by_region, scope)
    assert outcome.answer.status == "blocked", (
        f"SCD overlap must block, got {outcome.answer.status}: {rows_of(outcome)}"
    )
    assert any("double counted" in n for n in outcome.answer.notes), outcome.answer.notes
    assert "FAN_OUT_UNPROVEN" in outcome.answer.reason_codes


def test_scd_gap_blocks_rather_than_under_reporting(pipeline, scope, revenue_by_region):
    """An order in a coverage gap is silently dropped by the inner join. The coverage
    attestation must block instead of returning a quietly incomplete total."""
    outcome = pipeline("scd_gap").run(revenue_by_region, scope)
    assert outcome.answer.status == "blocked", (
        f"SCD gap must block, got {outcome.answer.status}: {rows_of(outcome)}"
    )
    assert any("under-report" in n for n in outcome.answer.notes), outcome.answer.notes


def test_restricted_principal_is_refused(pipeline, restricted_scope, revenue_by_region):
    outcome = pipeline("golden").run(revenue_by_region, restricted_scope)
    assert outcome.answer.status == "unavailable"
    assert "NOT_AUTHORIZED" in outcome.answer.reason_codes


def test_unsupported_grain_is_refused(pipeline, scope):
    request = SemanticRequest(
        operation=Operation.TREND, metric_ids=("finance.revenue",),
        dimension_ids=("customer.region_at_order",), time_range=Q2, time_grain="week",
    )
    outcome = pipeline("golden").run(request, scope)
    assert outcome.answer.status == "unavailable"
    assert "UNSUPPORTED_CAPABILITY" in outcome.answer.reason_codes


def test_multi_metric_request_is_refused_not_guessed(pipeline, scope):
    request = SemanticRequest(
        operation=Operation.AGGREGATE,
        metric_ids=("finance.revenue", "commerce.order_count"), time_range=Q2,
    )
    outcome = pipeline("golden").run(request, scope)
    assert outcome.answer.status == "unavailable"
    assert "UNSUPPORTED_CAPABILITY" in outcome.answer.reason_codes


def test_aggregate_without_a_dimension(pipeline, scope):
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("finance.revenue",), time_range=Q2
    )
    outcome = pipeline("golden").run(request, scope)
    assert outcome.answer.status == "answered", outcome.answer.notes
    assert Decimal(str(outcome.answer.rows[0][0])) == Decimal("250.00")


def test_order_count_metric_also_applies_the_mandatory_rule(pipeline, scope):
    """A different metric must still pull in the customer population rule."""
    request = SemanticRequest(
        operation=Operation.AGGREGATE, metric_ids=("commerce.order_count",), time_range=Q2
    )
    outcome = pipeline("golden").run(request, scope)
    assert outcome.answer.status == "answered", outcome.answer.notes
    assert "global.exclude_internal_customers@2" in outcome.answer.applied_rules
    # 4 qualifying orders. Unlike Revenue, order_count's population filters only on status
    # and is_test, so the fully refunded order legitimately counts. The internal customer,
    # test customer, test order and pending order are still excluded.
    assert int(outcome.answer.rows[0][0]) == 4


def test_execution_is_ledgered(pipeline, scope, revenue_by_region):
    pipe = pipeline("golden")
    outcome = pipe.run(revenue_by_region, scope)
    assert outcome.result.job_id in pipe.gateway.ledger.entries
    assert pipe.gateway.ledger.entries[outcome.result.job_id]["state"] == "completed"
