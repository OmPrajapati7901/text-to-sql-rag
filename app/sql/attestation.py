"""Temporal join uniqueness attestation.

DESIGN §40 makes this an execution prerequisite: before a metric is aggregated across an
as-of dimension join, something must prove that each candidate fact matches exactly one
dimension row.

This cannot be skipped on the grounds that the join "looks" many-to-one. An overlapping SCD
interval silently double counts, and the output grain stays perfectly unique while it happens
— an inner join and EXISTS both report success. A missing interval silently drops the fact.

This is the single targeted diagnostic query §19 permits, under the same authorization and
budget as the main query.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import expressions as exp

from app.catalog.service import CatalogService
from app.contracts.errors import Finding, ReasonCode, Severity
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import LogicalPlan
from app.sql.compiler import _and_all, _Ctx, _ParamBinder
from app.sql.dialects import duckdb as duckdb_dialect


@dataclass(frozen=True)
class Attestation:
    relationship_ref: str
    sql: str
    parameter_values: tuple[object, ...]
    overlapping_facts: int = 0
    unmatched_facts: int = 0

    @property
    def passed(self) -> bool:
        return self.overlapping_facts == 0 and self.unmatched_facts == 0


def build_attestations(
    plan: LogicalPlan,
    catalog: CatalogService,
    scope: TrustedScope,
    parameters: dict[str, object],
) -> list[Attestation]:
    """One attestation per join that declares a uniqueness contract."""
    out: list[Attestation] = []
    for join in plan.joins:
        rel = catalog.snapshot.get("relationships", join.relationship_ref.split("@")[0])
        if not rel.requires_uniqueness_contract:
            continue
        out.append(_build(plan, join, catalog, scope, parameters))
    return out


def _build(plan, join, catalog, scope, parameters) -> Attestation:
    binder = _ParamBinder()
    alias_of = {plan.base.table_id: plan.base.alias}
    for j in plan.joins:
        alias_of[j.right.table_id] = j.right.alias
    ctx = _Ctx(
        dialect=duckdb_dialect, alias_of=alias_of, binder=binder,
        supplied={**parameters, "p_tenant": scope.tenant_id}, catalog=catalog,
    )

    base_record = catalog.snapshot.get("tables", plan.base.table_id)
    grain_columns = base_record.grain or base_record.primary_key

    # Count, per fact row, how many dimension rows it matched. Anything other than exactly
    # one is a contract breach: >1 double counts, 0 silently drops the fact.
    # COUNT(*) would count the left row even when the LEFT JOIN matched nothing, reporting 1
    # for a fact that actually fell in a coverage gap. Count a NOT NULL column from the RIGHT
    # side instead, so an unmatched fact genuinely reads as zero.
    right_record = catalog.snapshot.get("tables", join.right.table_id)
    right_probe = next(
        (
            c for c in (right_record.grain or right_record.primary_key)
            if not catalog.snapshot.get("columns", c).nullable
        ),
        None,
    )
    if right_probe is None:
        raise ValueError(
            f"Cannot attest {join.relationship_ref}: {join.right.table_id} declares no "
            f"non-nullable grain column to probe"
        )

    inner = exp.Select()
    inner = inner.select(
        *[ctx.column(c) for c in grain_columns],
        exp.alias_(
            exp.Count(this=ctx.column(right_probe)),
            exp.to_identifier("match_count", quoted=True),
        ),
        append=False,
    )
    inner = inner.from_(
        duckdb_dialect.table_expression(plan.base.physical, plan.base.alias)
    )
    inner = inner.join(
        duckdb_dialect.table_expression(join.right.physical, join.right.alias),
        on=_and_all([ctx.predicate(p) for p in join.predicates]),
        join_type="left",  # LEFT so an unmatched fact is visible rather than vanishing.
    )
    # Only the metric's own population and time window matter here -- not the dimension-side
    # filters, which would mask an overlap by discarding one of the two matches.
    fact_only = [
        p for p in plan.where
        if _columns_of(p) and all(c.startswith(plan.base.table_id + ".") for c in _columns_of(p))
    ]
    if fact_only:
        inner = inner.where(_and_all([ctx.predicate(p) for p in fact_only]))
    inner = inner.group_by(*[ctx.column(c) for c in grain_columns], append=False)

    outer = exp.Select().select(
        exp.alias_(
            exp.Count(this=exp.Star()), exp.to_identifier("fact_rows", quoted=True)
        ),
        exp.alias_(
            exp.func(
                "sum",
                exp.Case()
                .when(
                    exp.GT(
                        this=exp.column("match_count", table="d", quoted=True),
                        expression=exp.Literal.number(1),
                    ),
                    exp.Literal.number(1),
                )
                .else_(exp.Literal.number(0)),
            ),
            exp.to_identifier("overlapping", quoted=True),
        ),
        exp.alias_(
            exp.func(
                "sum",
                exp.Case()
                .when(
                    exp.EQ(
                        this=exp.column("match_count", table="d", quoted=True),
                        expression=exp.Literal.number(0),
                    ),
                    exp.Literal.number(1),
                )
                .else_(exp.Literal.number(0)),
            ),
            exp.to_identifier("unmatched", quoted=True),
        ),
        append=False,
    ).from_(
        exp.Subquery(
            this=inner, alias=exp.TableAlias(this=exp.to_identifier("d", quoted=True))
        )
    )

    return Attestation(
        relationship_ref=join.relationship_ref,
        sql=outer.sql(dialect="duckdb", pretty=True),
        parameter_values=tuple(binder.values),
    )


def _columns_of(predicate) -> set[str]:
    from app.contracts.expressions import columns_in

    return set(columns_in(predicate))


def check(attestation: Attestation, overlapping: int, unmatched: int) -> Finding | None:
    """Turn attestation counts into a blocking finding.

    A LEFT JOIN that found zero matches means the metric would silently under-report; more
    than one means it would silently double count. Neither is an anomaly to warn about.
    """
    if overlapping:
        return Finding(
            code=ReasonCode.FAN_OUT_UNPROVEN,
            severity=Severity.BLOCK,
            message=(
                f"Uniqueness contract failed for {attestation.relationship_ref}: "
                f"{overlapping} fact row(s) match more than one dimension version. The "
                f"measure would be double counted, and the output grain would still look "
                f"unique."
            ),
            subject=attestation.relationship_ref,
        )
    if unmatched:
        return Finding(
            code=ReasonCode.RESULT_CONTRACT_BREACH,
            severity=Severity.BLOCK,
            message=(
                f"Coverage contract failed for {attestation.relationship_ref}: "
                f"{unmatched} fact row(s) match no dimension version. The metric would "
                f"silently under-report rather than fail."
            ),
            subject=attestation.relationship_ref,
        )
    return None
