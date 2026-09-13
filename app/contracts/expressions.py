"""Typed predicate and measure expression AST.

This is a closed vocabulary. A model can never insert a string that the compiler treats as an
expression: every node is a tagged union member, every literal is typed, and every column and
enum is a catalog reference resolved by the binder.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.ids import ObjectId


class ColumnExpr(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["column"] = "column"
    column_ref: ObjectId


class LiteralExpr(BaseModel):
    """A typed constant. Rendered as a bound parameter, never interpolated."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["literal"] = "literal"
    type: Literal["string", "boolean", "integer", "decimal", "instant", "date"]
    value: str | bool | int | float


class EnumExpr(BaseModel):
    """Reference into an authoritative value set: `values.order_status@2:completed`."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["enum"] = "enum"
    value_set_ref: str
    member: str


class ParameterExpr(BaseModel):
    """A named bound parameter supplied by trusted context or the parameter contract."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["parameter"] = "parameter"
    name: str = Field(pattern=r"^p_[a-z][a-z0-9_]*$")


Operand = Annotated[
    ColumnExpr | LiteralExpr | EnumExpr | ParameterExpr, Field(discriminator="kind")
]


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["compare"] = "compare"
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte"]
    left: Operand
    right: Operand


class IsNull(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["is_null"] = "is_null"
    negated: bool = False
    operand: Operand


class Membership(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["in"] = "in"
    negated: bool = False
    left: Operand
    members: tuple[Operand, ...] = Field(min_length=1)


class BoolOp(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["bool"] = "bool"
    op: Literal["and", "or", "not"]
    args: tuple[Predicate, ...] = Field(min_length=1)


Predicate = Annotated[
    Comparison | IsNull | Membership | BoolOp, Field(discriminator="kind")
]

BoolOp.model_rebuild()


class Aggregate(BaseModel):
    """An approved aggregate over a column. The operator set is closed."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["aggregate"] = "aggregate"
    op: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    operand: ColumnExpr | None = Field(
        default=None, description="None only for count(*)-style entity counts"
    )


class Ratio(BaseModel):
    """Aggregate numerator and denominator before dividing. Never an average of percentages."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["ratio"] = "ratio"
    numerator: MeasureExpr
    denominator: MeasureExpr
    zero_denominator: Literal["null", "zero", "error"] = "null"


MeasureExpr = Annotated[Aggregate | Ratio, Field(discriminator="kind")]

Ratio.model_rebuild()


def columns_in(node: Predicate | MeasureExpr | Operand | None) -> frozenset[str]:
    """Every column an expression touches — used so authorization inspects all references,
    not only projected output columns."""
    if node is None:
        return frozenset()
    match node:
        case ColumnExpr():
            return frozenset({node.column_ref})
        case LiteralExpr() | EnumExpr() | ParameterExpr():
            return frozenset()
        case Comparison():
            return columns_in(node.left) | columns_in(node.right)
        case IsNull():
            return columns_in(node.operand)
        case Membership():
            return columns_in(node.left) | frozenset().union(
                *(columns_in(m) for m in node.members)
            )
        case BoolOp():
            return frozenset().union(*(columns_in(a) for a in node.args))
        case Aggregate():
            return columns_in(node.operand)
        case Ratio():
            return columns_in(node.numerator) | columns_in(node.denominator)
    raise TypeError(f"Unknown expression node: {type(node).__name__}")
