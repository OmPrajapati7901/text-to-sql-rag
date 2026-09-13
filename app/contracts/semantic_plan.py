"""The three representations: intent sketch, bound semantic plan, logical plan.

`SemanticRequest` is the ONLY structure a model may author. It carries catalog IDs and
supported operators — no SQL fragments, no join predicates, no metric formulas, no free-form
identifiers. The binder resolves it into a `BoundSemanticPlan`; lowering produces a
`LogicalPlan`; the compiler renders that and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.coverage import CoverageMatrix
from app.contracts.expressions import MeasureExpr, Predicate
from app.contracts.ids import ObjectId, QualifiedName, SnapshotId
from app.contracts.semantics import TimeGrain


class Operation(StrEnum):
    LOOKUP = "lookup"
    AGGREGATE = "aggregate"
    TREND = "trend"
    RANK = "rank"
    COMPARE = "compare"


class TimeRange(BaseModel):
    """Half-open [start, end). Closed-open boundaries are the only supported form."""

    model_config = ConfigDict(frozen=True)
    start: datetime
    end_exclusive: datetime
    timezone: str = "UTC"
    grain: TimeGrain | None = None

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.end_exclusive <= self.start:
            raise ValueError("Time range end must be strictly after start")
        return self


class SortKey(BaseModel):
    model_config = ConfigDict(frozen=True)
    field: str
    direction: Literal["asc", "desc"] = "asc"


# --------------------------------------------------------------------------
# 1. What a model may propose
# --------------------------------------------------------------------------


class ProposedFilter(BaseModel):
    """A filter the model proposes. It names a dimension/column ID and a typed value; it can
    never supply an expression string."""

    model_config = ConfigDict(frozen=True)
    target_id: ObjectId
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"]
    values: tuple[str | bool | int | float, ...] = Field(min_length=1)


class SemanticRequest(BaseModel):
    """Model-authored proposal. Every field is an ID or an enumerated operator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    metric_ids: tuple[ObjectId, ...] = ()
    dimension_ids: tuple[ObjectId, ...] = ()
    filters: tuple[ProposedFilter, ...] = ()
    time_range: TimeRange | None = None
    time_grain: TimeGrain | None = None
    order_by: tuple[SortKey, ...] = ()
    limit: int | None = Field(default=None, ge=1, le=10_000)
    attribution: str | None = Field(
        default=None, description="Named, approved attribution policy; not free text SQL"
    )


class IntentSketch(BaseModel):
    """Language-level interpretation, before binding. Retains evidence spans and ambiguities."""

    model_config = ConfigDict(frozen=True)

    route: Literal["sql", "definition", "metadata", "clarify", "unsupported", "denied"]
    action: Operation | None = None
    normalized_question: str
    measure_phrases: tuple[str, ...] = ()
    dimension_phrases: tuple[str, ...] = ()
    filter_phrases: tuple[str, ...] = ()
    time_phrase: str | None = None
    unresolved_slots: tuple[str, ...] = ()
    evidence_spans: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 2. What the binder produces
# --------------------------------------------------------------------------


class BoundMetric(BaseModel):
    model_config = ConfigDict(frozen=True)
    ref: str
    expression: MeasureExpr
    population: Predicate | None
    base_table: ObjectId
    base_grain: tuple[ObjectId, ...]
    unit: str | None
    output_alias: str


class BoundDimension(BaseModel):
    model_config = ConfigDict(frozen=True)
    ref: str
    column: ObjectId
    output_alias: str
    entity_role: str | None = None
    temporal_binding: str | None = None
    via_relationship_ref: str | None = None


class BoundTimeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    column: ObjectId
    range: TimeRange
    grain: TimeGrain | None
    output_alias: str | None = None
    calendar_ref: str = "calendar.gregorian@1"
    partial_period_policy: Literal["require_complete", "allow_partial"] = "allow_partial"


class Obligation(BaseModel):
    """A mandatory rule effect with its proof of placement."""

    model_config = ConfigDict(frozen=True)
    rule_ref: str
    rule_class: str
    predicate: Predicate | None = None
    placement: Literal["before_aggregation", "after_aggregation"] = "before_aggregation"
    required_tables: tuple[ObjectId, ...] = ()
    required_relationship_ref: str | None = None
    satisfied_by: str | None = Field(
        default=None, description="Set by the compiler: where the effect was emitted"
    )


class ExpectedResultContract(BaseModel):
    """Attached before SQL generation, checked after execution."""

    model_config = ConfigDict(frozen=True)
    columns: tuple[str, ...]
    grain: tuple[str, ...]
    unit: str | None = None
    unique_keys: tuple[str, ...] = ()
    missing_groups: Literal["omit", "fill"] = "omit"
    empty_set_policy: str = "no_matching_data"
    max_rows: int = 10_000


class BoundSemanticPlan(BaseModel):
    """The durable interpretation contract. Every reference is pinned and authorized."""

    model_config = ConfigDict(frozen=True)

    ir_version: Literal["1.0"] = "1.0"
    plan_id: str
    plan_revision: int = Field(default=1, ge=1)
    operation: Operation
    snapshot_id: SnapshotId
    policy_epoch: int

    metrics: tuple[BoundMetric, ...] = ()
    dimensions: tuple[BoundDimension, ...] = ()
    time: BoundTimeSpec | None = None
    filters: tuple[Predicate, ...] = ()
    obligations: tuple[Obligation, ...] = ()

    relationship_refs: tuple[str, ...] = ()
    rule_refs: tuple[str, ...] = ()
    metric_refs: tuple[str, ...] = ()

    order_by: tuple[SortKey, ...] = ()
    limit: int | None = None
    result_contract: ExpectedResultContract
    coverage: CoverageMatrix = Field(default_factory=CoverageMatrix)
    evidence_refs: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 3. Compiler input
# --------------------------------------------------------------------------


class ScanOp(BaseModel):
    model_config = ConfigDict(frozen=True)
    table_id: ObjectId
    physical: QualifiedName
    alias: str
    columns: tuple[ObjectId, ...]


class JoinOp(BaseModel):
    model_config = ConfigDict(frozen=True)
    relationship_ref: str
    left_alias: str
    right_alias: str
    right: ScanOp
    join_type: Literal["inner", "left"]
    predicates: tuple[Predicate, ...]
    cardinality: str
    preserves_left_multiplicity: bool = Field(
        description="Proven many-to-one at the joined key; false blocks additive aggregation"
    )


class LogicalPlan(BaseModel):
    """Typed operators with schema, grain and lineage. The only compiler input."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_revision: int
    dialect: str
    base: ScanOp
    joins: tuple[JoinOp, ...] = ()
    where: tuple[Predicate, ...] = ()
    group_by: tuple[tuple[str, ObjectId | None], ...] = Field(
        default=(), description="(output_alias, source column ID or None for a time bucket)"
    )
    time_bucket: tuple[str, ObjectId, TimeGrain, str] | None = Field(
        default=None, description="(alias, column ID, grain, timezone)"
    )
    measures: tuple[tuple[str, MeasureExpr], ...] = ()
    having: tuple[Predicate, ...] = ()
    order_by: tuple[SortKey, ...] = ()
    limit: int | None = None
    output_grain: tuple[str, ...] = ()
    lineage: dict[str, str] = Field(
        default_factory=dict, description="output alias -> originating ref"
    )


class CompiledQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_revision: int
    dialect: str
    sql: str
    parameter_values: tuple[object, ...] = Field(
        description="Bound values in placeholder order. Positional, so names may repeat."
    )
    parameter_order: tuple[str, ...] = Field(
        description="Name of each bound value, for audit. Not a unique key."
    )
    referenced_tables: frozenset[ObjectId]
    referenced_columns: frozenset[ObjectId]
    lineage: dict[str, str]
    compiler_version: str
