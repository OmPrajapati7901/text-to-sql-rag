"""Lower a bound semantic plan into a typed logical plan.

Every operator carries its schema, grain and lineage. This is the last representation before
SQL, and the only thing the compiler accepts.
"""

from __future__ import annotations

from app.catalog.service import CatalogService
from app.contracts.errors import GovernedError, ReasonCode
from app.contracts.expressions import (
    BoolOp,
    ColumnExpr,
    Comparison,
    IsNull,
    ParameterExpr,
    Predicate,
)
from app.contracts.ids import table_id_of
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import (
    BoundSemanticPlan,
    JoinOp,
    LogicalPlan,
    ScanOp,
    SortKey,
)
from app.relationships.planner import JoinPlan

# Join predicate operators, as stored on approved relationship records.
_PRED_OPS = {"eq": "eq", "gte": "gte", "lt": "lt", "lte": "lte", "gt": "gt"}


class Lowering:
    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog

    def lower(
        self,
        plan: BoundSemanticPlan,
        join_plan: JoinPlan,
        scope: TrustedScope,
        *,
        dialect: str,
    ) -> LogicalPlan:
        aliases = self._assign_aliases(join_plan)
        base_table = self.catalog.get_table(join_plan.base_table, scope)
        base_scan = ScanOp(
            table_id=base_table.id,
            physical=base_table.physical,
            alias=aliases[base_table.id],
            columns=tuple(sorted(self._columns_for(plan, base_table.id))),
        )

        joins: list[JoinOp] = []
        for planned in join_plan.joins:
            right = self.catalog.get_table(planned.to_table, scope)
            joins.append(
                JoinOp(
                    relationship_ref=str(planned.relationship.ref),
                    left_alias=aliases[planned.from_table],
                    right_alias=aliases[planned.to_table],
                    right=ScanOp(
                        table_id=right.id,
                        physical=right.physical,
                        alias=aliases[right.id],
                        columns=tuple(sorted(self._columns_for(plan, right.id))),
                    ),
                    join_type=planned.relationship.join_type,
                    predicates=self._join_predicates(planned),
                    cardinality=planned.relationship.cardinality.value,
                    preserves_left_multiplicity=planned.preserves_left_multiplicity,
                )
            )

        where: list[Predicate] = []

        # 1. Tenant scope, bound from trusted context -- never from the request payload.
        scanned = [base_table] + [
            self.catalog.get_table(j.to_table, scope) for j in join_plan.joins
        ]
        needs_tenant = any(
            o.rule_ref.startswith("security.tenant_scope") for o in plan.obligations
        )
        if needs_tenant:
            for table in scanned:
                if "tenant_scoped" in table.tags:
                    if not table.tenant_key:
                        raise GovernedError(
                            ReasonCode.VALIDATION_FAILED,
                            f"Table {table.id} is tagged tenant_scoped but declares no tenant "
                            f"key; the isolation obligation cannot be placed.",
                            subject=table.id,
                        )
                    where.append(
                        Comparison(
                            op="eq",
                            left=ColumnExpr(column_ref=table.tenant_key),
                            right=ParameterExpr(name="p_tenant"),
                        )
                    )

        # 2. Time interval, half-open.
        if plan.time:
            where.append(
                Comparison(
                    op="gte",
                    left=ColumnExpr(column_ref=plan.time.column),
                    right=ParameterExpr(name="p_time_start"),
                )
            )
            where.append(
                Comparison(
                    op="lt",
                    left=ColumnExpr(column_ref=plan.time.column),
                    right=ParameterExpr(name="p_time_end"),
                )
            )

        # 3. Metric population.
        for metric in plan.metrics:
            if metric.population is not None:
                where.append(metric.population)

        # 4. Mandatory rule obligations, at their declared placement.
        for obligation in plan.obligations:
            if obligation.predicate is None:
                continue
            if obligation.placement != "before_aggregation":
                continue
            where.append(obligation.predicate)

        # 5. User filters.
        where.extend(plan.filters)

        having = [
            o.predicate
            for o in plan.obligations
            if o.predicate is not None and o.placement == "after_aggregation"
        ]

        # -- grouping -----------------------------------------------------
        group_by: list[tuple[str, str | None]] = []
        time_bucket = None
        if plan.time and plan.time.grain and plan.time.output_alias:
            # The governed reporting timezone, resolved by the default rule during closure --
            # never the machine's local timezone.
            time_bucket = (
                plan.time.output_alias,
                plan.time.column,
                plan.time.grain,
                plan.time.range.timezone,
            )
            group_by.append((plan.time.output_alias, None))
        for dim in plan.dimensions:
            group_by.append((dim.output_alias, dim.column))

        measures = tuple((m.output_alias, m.expression) for m in plan.metrics)

        lineage: dict[str, str] = {}
        if time_bucket:
            lineage[time_bucket[0]] = f"{plan.time.column}#{plan.time.grain}"
        for dim in plan.dimensions:
            lineage[dim.output_alias] = dim.ref
        for metric in plan.metrics:
            lineage[metric.output_alias] = metric.ref

        order_by = tuple(
            SortKey(field=s.field, direction=s.direction)
            if not isinstance(s, dict)
            else SortKey(**s)
            for s in plan.order_by
        )

        return LogicalPlan(
            plan_id=plan.plan_id,
            plan_revision=plan.plan_revision,
            dialect=dialect,
            base=base_scan,
            joins=tuple(joins),
            where=tuple(where),
            group_by=tuple(group_by),
            time_bucket=time_bucket,
            measures=measures,
            having=tuple(having),
            order_by=order_by,
            limit=plan.limit,
            output_grain=tuple(a for a, _ in group_by),
            lineage=lineage,
        )

    def _assign_aliases(self, join_plan: JoinPlan) -> dict[str, str]:
        """Aliases are generated by code so a catalog name can never collide with SQL syntax."""
        ordered = [join_plan.base_table] + [j.to_table for j in join_plan.joins]
        return {table_id: f"t{i}" for i, table_id in enumerate(ordered)}

    def _columns_for(self, plan: BoundSemanticPlan, table_id: str) -> set[str]:
        from app.contracts.expressions import columns_in

        wanted: set[str] = set()
        for metric in plan.metrics:
            wanted |= columns_in(metric.expression) | columns_in(metric.population)
        for dim in plan.dimensions:
            wanted.add(dim.column)
        if plan.time:
            wanted.add(plan.time.column)
        for pred in plan.filters:
            wanted |= columns_in(pred)
        for obligation in plan.obligations:
            wanted |= columns_in(obligation.predicate)
        return {c for c in wanted if table_id_of(c) == table_id}

    def _join_predicates(self, planned) -> tuple[Predicate, ...]:
        out: list[Predicate] = []
        for pred in planned.relationship.predicates:
            left = ColumnExpr(column_ref=pred.left_column)
            right = ColumnExpr(column_ref=pred.right_column)
            if pred.op in _PRED_OPS:
                out.append(Comparison(op=_PRED_OPS[pred.op], left=left, right=right))
            elif pred.op == "lt_or_null":
                # Open-ended SCD interval: fact < valid_to OR valid_to IS NULL.
                out.append(
                    BoolOp(
                        op="or",
                        args=(
                            Comparison(op="lt", left=left, right=right),
                            IsNull(operand=right),
                        ),
                    )
                )
            else:
                raise GovernedError(
                    ReasonCode.UNSUPPORTED_OPERATOR,
                    f"Approved relationship uses unsupported predicate operator {pred.op!r}",
                    subject=str(planned.relationship.ref),
                )
        return tuple(out)
