"""Metric, dimension and rule contracts.

A metric has a *public contract* (what the model may see) and a *protected implementation*
(bound measures and predicates the compiler resolves). A user allowed to see aggregate Revenue
does not automatically gain access to its underlying amount column.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.catalog import RecordBase
from app.contracts.expressions import MeasureExpr, Predicate
from app.contracts.ids import ObjectId, ObjectType


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class MetricTimeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    time_column: ObjectId
    grains: tuple[TimeGrain, ...]
    timezone: str = "UTC"
    calendar_ref: str = "calendar.gregorian@1"


class MetricRecord(RecordBase):
    object_type: ObjectType = ObjectType.METRIC

    # --- public contract -------------------------------------------------
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    unit: str | None = None
    base_entity: str
    allowed_dimensions: tuple[ObjectId, ...] = ()
    empty_set_policy: Literal["no_matching_data", "zero"] = "no_matching_data"
    missing_groups: Literal["omit", "fill"] = "omit"
    additivity: dict[str, str] = Field(default_factory=dict)

    # --- protected implementation ---------------------------------------
    base_table: ObjectId
    base_grain: tuple[ObjectId, ...]
    expression: MeasureExpr
    population: Predicate | None = None
    required_rule_refs: tuple[str, ...] = ()
    time: MetricTimeSpec
    dependency_refs: tuple[str, ...] = ()
    supersedes: str | None = None


class DimensionRecord(RecordBase):
    object_type: ObjectType = ObjectType.DIMENSION
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    column: ObjectId = Field(description="The attribute column this dimension exposes")
    entity_role: str | None = None
    temporal_binding: Literal["current", "at_fact_event"] | None = None
    required_relationship_ref: str | None = Field(
        default=None, description="Edge that reaches the attribute from the fact"
    )


class RuleClass(StrEnum):
    """Scope and precedence are different concepts. A user preference never overrides tenant
    isolation; a table default never redefines a certified metric."""

    SECURITY_OBLIGATION = "security_obligation"
    MANDATORY_BUSINESS_POPULATION = "mandatory_business_population"
    SEMANTIC_REQUIREMENT = "semantic_requirement"
    CONTEXTUAL = "contextual"
    DEFAULT = "default"
    PREFERENCE = "preference"


# Lower number wins. Security is non-compensable.
RULE_CLASS_PRECEDENCE: dict[RuleClass, int] = {
    RuleClass.SECURITY_OBLIGATION: 0,
    RuleClass.MANDATORY_BUSINESS_POPULATION: 1,
    RuleClass.SEMANTIC_REQUIREMENT: 2,
    RuleClass.CONTEXTUAL: 3,
    RuleClass.DEFAULT: 4,
    RuleClass.PREFERENCE: 5,
}


class Activation(BaseModel):
    """Indexed activation predicate, evaluated deterministically against the plan.

    Negative activations that could oscillate during closure are rejected at publication.
    """

    model_config = ConfigDict(frozen=True)
    op: Literal[
        "any_scan_with_tag",
        "population_contains_entity",
        "uses_table",
        "uses_column",
        "uses_metric",
        "slot_unset",
        "always",
    ]
    tag: str | None = None
    entity: str | None = None
    object_id: ObjectId | None = None
    metric_ref: str | None = None
    slot: str | None = None


class RuleEffect(BaseModel):
    model_config = ConfigDict(frozen=True)
    op: Literal[
        "require_population_filter",
        "require_tenant_scope",
        "set_slot",
        "require_semijoin_or_allocation",
        "prefer_relationship",
    ]
    predicate: Predicate | None = None
    placement: Literal["before_aggregation", "after_aggregation"] = "before_aggregation"
    required_tables: tuple[ObjectId, ...] = ()
    required_relationship_ref: str | None = None
    temporal_binding: Literal["current", "at_fact_event"] | None = None
    slot: str | None = None
    slot_value: str | None = None
    unmatched_policy: Literal["quality_failure", "drop", "keep"] = "quality_failure"


class RuleRecord(RecordBase):
    object_type: ObjectType = ObjectType.RULE
    name: str
    description: str = ""
    rule_class: RuleClass
    activation: Activation
    effect: RuleEffect
    overrideable: bool = False
    allowed_override: str | None = None
    failure: Literal["deny", "block_metric", "clarify_or_unavailable", "warn"] = "deny"
    priority_within_class: int = 100

    @model_validator(mode="after")
    def _security_is_not_overrideable(self) -> RuleRecord:
        if self.rule_class is RuleClass.SECURITY_OBLIGATION and self.overrideable:
            raise ValueError(f"Security rule {self.id!r} cannot be overrideable")
        return self

    @property
    def precedence(self) -> tuple[int, int]:
        return (RULE_CLASS_PRECEDENCE[self.rule_class], self.priority_within_class)

    @property
    def is_mandatory(self) -> bool:
        return self.rule_class in (
            RuleClass.SECURITY_OBLIGATION,
            RuleClass.MANDATORY_BUSINESS_POPULATION,
            RuleClass.SEMANTIC_REQUIREMENT,
        )
