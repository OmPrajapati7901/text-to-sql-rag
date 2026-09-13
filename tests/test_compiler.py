"""Compiler output and the validation gates.

The tamper tests are the important ones: they modify the emitted SQL *without* changing the
plan that produced it, which is the only way to prove the validators read the text rather
than re-checking the plan against itself.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import expressions as exp

pytestmark = pytest.mark.unit

Q = '"'


@pytest.fixture
def compiled_golden(services, scope, revenue_by_region):
    bound = services.binder.bind(revenue_by_region, scope, plan_id="t_compile")
    logical = services.lowering.lower(
        bound.plan, bound.join_plan, scope, dialect=services.dialect
    )
    compiled = services.compiler.compile(
        logical, scope,
        parameters={
            "p_time_start": bound.plan.time.range.start,
            "p_time_end": bound.plan.time.range.end_exclusive,
        },
    )
    return bound, logical, compiled


def test_compiles_to_valid_parseable_sql(compiled_golden):
    _, _, compiled = compiled_golden
    parsed = sqlglot.parse(compiled.sql, dialect="duckdb")
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select)


def test_all_values_are_bound_parameters(compiled_golden):
    _, _, compiled = compiled_golden
    assert compiled.sql.count("?") == len(compiled.parameter_values)
    assert "'completed'" not in compiled.sql, "enum must be bound, not inlined"
    assert "tenant-demo" not in compiled.sql, "tenant must be bound, not inlined"


def test_identifiers_are_quoted_and_qualified(compiled_golden):
    _, _, compiled = compiled_golden
    assert f'{Q}warehouse{Q}.{Q}commerce{Q}.{Q}orders{Q}' in compiled.sql
    assert "SELECT *" not in compiled.sql


def test_temporal_join_carries_the_full_approved_predicate(compiled_golden):
    _, _, compiled = compiled_golden
    sql = compiled.sql
    assert f'{Q}t0{Q}.{Q}tenant_id{Q} = {Q}t1{Q}.{Q}tenant_id{Q}' in sql
    assert f'{Q}t0{Q}.{Q}customer_id{Q} = {Q}t1{Q}.{Q}customer_id{Q}' in sql
    assert f'{Q}t0{Q}.{Q}ordered_at{Q} >= {Q}t1{Q}.{Q}valid_from{Q}' in sql
    assert "IS NULL" in sql, "open-ended SCD interval must be preserved"


def test_mandatory_rule_predicates_are_emitted(compiled_golden):
    _, _, compiled = compiled_golden
    assert f'{Q}t1{Q}.{Q}is_internal{Q}' in compiled.sql
    assert f'{Q}t1{Q}.{Q}is_test_customer{Q}' in compiled.sql


def test_tenant_isolation_covers_every_scanned_table(compiled_golden):
    _, _, compiled = compiled_golden
    assert compiled.sql.count(f'{Q}tenant_id{Q} = ?') == 2


def test_time_interval_is_half_open(compiled_golden):
    _, _, compiled = compiled_golden
    assert f'{Q}t0{Q}.{Q}ordered_at{Q} >= ?' in compiled.sql
    assert f'{Q}t0{Q}.{Q}ordered_at{Q} < ?' in compiled.sql


def test_lineage_is_recorded_for_every_projection(compiled_golden):
    _, _, compiled = compiled_golden
    assert compiled.lineage["revenue_usd"] == "finance.revenue@7"
    assert compiled.lineage["region_at_order"] == "customer.region_at_order@2"


def test_valid_plan_passes_every_gate(services, scope, compiled_golden):
    bound, logical, compiled = compiled_golden
    report = services.validator.validate(compiled, logical, bound.plan, scope)
    assert report.passed, [str(f) for f in report.findings]


# --- tamper tests: modify only the SQL text, never the plan ------------------

TAMPERS = [
    (
        "mandatory rule predicate removed",
        lambda s: s.replace(f'\n  AND {Q}t1{Q}.{Q}is_internal{Q} = ?', ""),
        "MANDATORY_RULE_UNRESOLVED",
    ),
    (
        "tenant isolation removed",
        lambda s: s.replace(f'{Q}t0{Q}.{Q}tenant_id{Q} = ?\n  AND ', ""),
        "MANDATORY_RULE_UNRESOLVED",
    ),
    (
        "second statement appended",
        lambda s: s + "; DROP TABLE warehouse.commerce.orders",
        "UNSAFE_QUERY",
    ),
    (
        "SUM(DISTINCT) used as a fan-out repair",
        lambda s: s.replace(
            f'SUM({Q}t0{Q}.{Q}order_amount_usd{Q})',
            f'SUM(DISTINCT {Q}t0{Q}.{Q}order_amount_usd{Q})',
        ),
        "VALIDATION_FAILED",
    ),
    (
        "literal inlined instead of bound",
        lambda s: s.replace(
            f'{Q}t0{Q}.{Q}order_status{Q} = ?', f"{Q}t0{Q}.{Q}order_status{Q} = 'completed'"
        ),
        "UNSAFE_QUERY",
    ),
    (
        "swapped to a non-certified table",
        lambda s: s.replace(
            f'{Q}warehouse{Q}.{Q}commerce{Q}.{Q}customer_history{Q}',
            f'{Q}warehouse{Q}.{Q}commerce{Q}.{Q}products{Q}',
        ),
        "UNSUPPORTED_CAPABILITY",
    ),
    (
        "forbidden function substituted",
        lambda s: s.replace("DATE_TRUNC", "READ_CSV"),
        "UNSAFE_QUERY",
    ),
    (
        "partial join key (tenant dropped)",
        lambda s: s.replace(
            f'ON {Q}t0{Q}.{Q}tenant_id{Q} = {Q}t1{Q}.{Q}tenant_id{Q}\n  AND ', "ON "
        ),
        "VALIDATION_FAILED",
    ),
    (
        "SCD open-interval clause dropped",
        lambda s: s.replace(
            f'  AND (\n    {Q}t0{Q}.{Q}ordered_at{Q} < {Q}t1{Q}.{Q}valid_to{Q} '
            f'OR {Q}t1{Q}.{Q}valid_to{Q} IS NULL\n  )\n',
            "",
        ),
        "VALIDATION_FAILED",
    ),
]


@pytest.mark.parametrize("label,mutate,expected_code", TAMPERS, ids=[t[0] for t in TAMPERS])
def test_tampered_sql_is_blocked(services, scope, compiled_golden, label, mutate, expected_code):
    bound, logical, compiled = compiled_golden
    mutated = mutate(compiled.sql)
    assert mutated != compiled.sql, f"tamper {label!r} did not change the SQL"

    report = services.validator.validate(
        compiled.model_copy(update={"sql": mutated}), logical, bound.plan, scope
    )
    assert not report.passed, f"{label} was not blocked"
    codes = {f.code.value for f in report.blocking}
    assert expected_code in codes, f"{label}: expected {expected_code}, got {sorted(codes)}"


def test_a_failing_gate_blocks_rather_than_crashes(services, scope, compiled_golden):
    """A validator that cannot complete has proven nothing, so it must fail closed."""
    bound, logical, compiled = compiled_golden
    broken = compiled.model_copy(update={"sql": "SELECT ?? FROM nowhere WHERE"})
    report = services.validator.validate(broken, logical, bound.plan, scope)
    assert not report.passed
