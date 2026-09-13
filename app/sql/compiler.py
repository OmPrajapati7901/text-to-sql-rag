"""Deterministic SQL compiler.

Input is a LogicalPlan and nothing else. Values become bound parameters; identifiers are
resolved and quoted by the compiler from catalog records. There is no code path by which a
string authored elsewhere becomes part of an expression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlglot import expressions as exp

from app.catalog.service import CatalogService
from app.contracts.errors import CompilationError, ReasonCode
from app.contracts.expressions import (
    Aggregate,
    BoolOp,
    ColumnExpr,
    Comparison,
    EnumExpr,
    IsNull,
    LiteralExpr,
    Membership,
    ParameterExpr,
    Ratio,
)
from app.contracts.ids import table_id_of
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import CompiledQuery, LogicalPlan
from app.sql.dialects import duckdb as duckdb_dialect

_DIALECTS = {"duckdb": duckdb_dialect}

_COMPARISON_NODES = {
    "eq": exp.EQ, "ne": exp.NEQ, "lt": exp.LT,
    "lte": exp.LTE, "gt": exp.GT, "gte": exp.GTE,
}


@dataclass
class _ParamBinder:
    """Collects bound parameters in emission order. Values never reach the SQL text."""

    values: list[object] = field(default_factory=list)
    names: list[str] = field(default_factory=list)

    def bind(self, name: str, value: object) -> exp.Placeholder:
        self.names.append(name)
        self.values.append(value)
        return exp.Placeholder()


class SqlCompiler:
    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog

    def compile(
        self,
        plan: LogicalPlan,
        scope: TrustedScope,
        *,
        parameters: dict[str, object] | None = None,
    ) -> CompiledQuery:
        dialect = _DIALECTS.get(plan.dialect)
        if dialect is None:
            raise CompilationError(
                ReasonCode.UNSUPPORTED_CAPABILITY,
                f"No certified adapter for dialect {plan.dialect!r}",
                subject=plan.dialect,
            )

        supplied = dict(parameters or {})
        supplied.setdefault("p_tenant", scope.tenant_id)
        binder = _ParamBinder()

        alias_of: dict[str, str] = {plan.base.table_id: plan.base.alias}
        for join in plan.joins:
            alias_of[join.right.table_id] = join.right.alias

        ctx = _Ctx(dialect=dialect, alias_of=alias_of, binder=binder,
                   supplied=supplied, catalog=self.catalog)

        select = exp.Select()

        # -- projections, in contract order ------------------------------
        projections: list[exp.Expression] = []
        if plan.time_bucket:
            alias, column_id, grain, timezone = plan.time_bucket
            projections.append(
                exp.alias_(
                    dialect.time_bucket(ctx.column(column_id), grain, timezone),
                    exp.to_identifier(alias, quoted=True),
                )
            )
        for out_alias, column_id in plan.group_by:
            if column_id is None:
                continue
            projections.append(
                exp.alias_(ctx.column(column_id), exp.to_identifier(out_alias, quoted=True))
            )
        for out_alias, measure in plan.measures:
            projections.append(
                exp.alias_(ctx.measure(measure), exp.to_identifier(out_alias, quoted=True))
            )
        if not projections:
            raise CompilationError(
                ReasonCode.COMPILER_DEFECT, "Plan produced no projections", subject=plan.plan_id
            )
        select = select.select(*projections, append=False)

        # -- from / joins -------------------------------------------------
        select = select.from_(dialect.table_expression(plan.base.physical, plan.base.alias))
        for join in plan.joins:
            on = _and_all([ctx.predicate(p) for p in join.predicates])
            select = select.join(
                dialect.table_expression(join.right.physical, join.right.alias),
                on=on,
                join_type=join.join_type,
            )

        # -- where / group / having / order -------------------------------
        if plan.where:
            select = select.where(_and_all([ctx.predicate(p) for p in plan.where]))

        group_exprs: list[exp.Expression] = []
        if plan.time_bucket:
            alias, column_id, grain, timezone = plan.time_bucket
            group_exprs.append(dialect.time_bucket(ctx.column(column_id), grain, timezone))
        for _, column_id in plan.group_by:
            if column_id is not None:
                group_exprs.append(ctx.column(column_id))
        if group_exprs:
            select = select.group_by(*group_exprs, append=False)

        if plan.having:
            select = select.having(_and_all([ctx.predicate(p) for p in plan.having]))

        if plan.order_by:
            ordered = [
                exp.Ordered(
                    this=exp.to_identifier(key.field, quoted=True),
                    desc=key.direction == "desc",
                )
                for key in plan.order_by
            ]
            select = select.order_by(*ordered, append=False)

        if plan.limit is not None:
            select = select.limit(plan.limit)

        sql = select.sql(dialect=plan.dialect, pretty=True)

        referenced_columns = frozenset(ctx.referenced_columns)
        referenced_tables = frozenset(table_id_of(c) for c in referenced_columns) | {
            plan.base.table_id, *(j.right.table_id for j in plan.joins)
        }

        # The placeholder count in the rendered SQL must equal the bound value count, or
        # execution would silently misalign values with predicates.
        placeholders = len(list(select.find_all(exp.Placeholder)))
        if placeholders != len(binder.values):
            raise CompilationError(
                ReasonCode.COMPILER_DEFECT,
                f"Compiler emitted {placeholders} placeholder(s) but bound "
                f"{len(binder.values)} value(s)",
                subject=plan.plan_id,
            )

        return CompiledQuery(
            plan_id=plan.plan_id,
            plan_revision=plan.plan_revision,
            dialect=plan.dialect,
            sql=sql,
            parameter_values=tuple(binder.values),
            parameter_order=tuple(binder.names),
            referenced_tables=referenced_tables,
            referenced_columns=referenced_columns,
            lineage=dict(plan.lineage),
            compiler_version=dialect.COMPILER_VERSION,
        )


@dataclass
class _Ctx:
    dialect: object
    alias_of: dict[str, str]
    binder: _ParamBinder
    supplied: dict[str, object]
    catalog: CatalogService
    referenced_columns: set[str] = field(default_factory=set)

    def column(self, column_id: str) -> exp.Column:
        table_id = table_id_of(column_id)
        alias = self.alias_of.get(table_id)
        if alias is None:
            raise CompilationError(
                ReasonCode.UNRESOLVED_REFERENCE,
                f"Column {column_id} belongs to {table_id}, which this plan does not scan",
                subject=column_id,
            )
        record = self.catalog.snapshot.get("columns", column_id)
        self.referenced_columns.add(column_id)
        return self.dialect.column_expression(alias, record.name)

    def operand(self, node) -> exp.Expression:
        match node:
            case ColumnExpr():
                return self.column(node.column_ref)
            case LiteralExpr():
                return self.binder.bind("literal", _coerce(node))
            case EnumExpr():
                # Resolved through the authoritative value set; never a model-written string.
                value = self.catalog.resolve_enum(node.value_set_ref, node.member)
                return self.binder.bind(f"enum_{node.member}", value)
            case ParameterExpr():
                if node.name not in self.supplied:
                    raise CompilationError(
                        ReasonCode.UNRESOLVED_REFERENCE,
                        f"Plan references unbound parameter {node.name!r}",
                        subject=node.name,
                    )
                return self.binder.bind(node.name, self.supplied[node.name])
        raise CompilationError(
            ReasonCode.UNSUPPORTED_OPERATOR, f"Unsupported operand {type(node).__name__}"
        )

    def predicate(self, node) -> exp.Expression:
        match node:
            case Comparison():
                cls = _COMPARISON_NODES[node.op]
                return cls(this=self.operand(node.left), expression=self.operand(node.right))
            case IsNull():
                inner = exp.Is(this=self.operand(node.operand), expression=exp.Null())
                return exp.Not(this=inner) if node.negated else inner
            case Membership():
                members = [self.operand(m) for m in node.members]
                inner = exp.In(this=self.operand(node.left), expressions=members)
                return exp.Not(this=inner) if node.negated else inner
            case BoolOp():
                parts = [self.predicate(a) for a in node.args]
                if node.op == "not":
                    if len(parts) != 1:
                        raise CompilationError(
                            ReasonCode.COMPILER_DEFECT, "NOT takes exactly one argument"
                        )
                    return exp.Not(this=exp.Paren(this=parts[0]))
                joiner = exp.And if node.op == "and" else exp.Or
                combined = parts[0]
                for part in parts[1:]:
                    combined = joiner(this=combined, expression=part)
                return exp.Paren(this=combined) if node.op == "or" else combined
        raise CompilationError(
            ReasonCode.UNSUPPORTED_OPERATOR, f"Unsupported predicate {type(node).__name__}"
        )

    def measure(self, node) -> exp.Expression:
        match node:
            case Aggregate():
                operand = self.column(node.operand.column_ref) if node.operand else None
                return self.dialect.aggregate(node.op, operand)
            case Ratio():
                num, den = self.measure(node.numerator), self.measure(node.denominator)
                if node.zero_denominator == "null":
                    # Explicit zero-denominator behaviour from the metric contract.
                    return exp.Div(
                        this=num,
                        expression=exp.func("nullif", den, exp.Literal.number(0)),
                    )
                return exp.Div(this=num, expression=den)
        raise CompilationError(
            ReasonCode.UNSUPPORTED_OPERATOR, f"Unsupported measure {type(node).__name__}"
        )


def _and_all(parts: list[exp.Expression]) -> exp.Expression:
    combined = parts[0]
    for part in parts[1:]:
        combined = exp.And(this=combined, expression=part)
    return combined


def _coerce(node: LiteralExpr) -> object:
    if node.type == "instant" and isinstance(node.value, str):
        return datetime.fromisoformat(node.value)
    return node.value
