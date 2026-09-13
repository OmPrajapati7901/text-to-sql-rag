"""DuckDB dialect adapter and capability manifest.

The manifest is an allowlist. An operator or function absent from it is not lowered — it is
rejected, so unsupported analytics become an explicit response rather than improvised SQL.
"""

from __future__ import annotations

from sqlglot import expressions as exp

from app.contracts.errors import CompilationError, ReasonCode
from app.contracts.ids import QualifiedName
from app.contracts.semantics import TimeGrain

NAME = "duckdb"
COMPILER_VERSION = "duckdb-adapter-1"

SUPPORTED_AGGREGATES = {"sum", "count", "count_distinct", "avg", "min", "max"}
SUPPORTED_GRAINS = {
    TimeGrain.DAY: "day",
    TimeGrain.WEEK: "week",
    TimeGrain.MONTH: "month",
    TimeGrain.QUARTER: "quarter",
    TimeGrain.YEAR: "year",
}
SUPPORTED_FUNCTIONS = {"date_trunc"}

# Anything that reads or writes outside the query, or executes arbitrary code, is refused
# even inside an otherwise read-only SELECT.
FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_blob", "read_text", "glob", "copy", "install", "load", "attach",
    "httpfs", "url", "shell", "system", "getenv", "sniff_csv", "parquet_scan",
}


def table_expression(physical: QualifiedName, alias: str) -> exp.Table:
    """Fully qualified, compiler-quoted. The logical `warehouse` database is ATTACHed under
    that name by the gateway, so all three parts resolve."""
    return exp.Table(
        catalog=exp.to_identifier(physical.database, quoted=True),
        db=exp.to_identifier(physical.schema_name, quoted=True),
        this=exp.to_identifier(physical.name, quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)),
    )


def column_expression(alias: str, column_name: str) -> exp.Column:
    return exp.Column(
        this=exp.to_identifier(column_name, quoted=True),
        table=exp.to_identifier(alias, quoted=True),
    )


def time_bucket(column: exp.Expression, grain: TimeGrain, timezone: str) -> exp.Expression:
    """date_trunc at the governed reporting timezone.

    Values are stored as TIMESTAMPTZ (an absolute instant). Bucketing a calendar period is a
    wall-clock question, so the instant is converted into the reporting zone first. For UTC
    the conversion is an identity, but making it explicit keeps a non-UTC calendar honest.
    """
    if grain not in SUPPORTED_GRAINS:
        raise CompilationError(
            ReasonCode.UNSUPPORTED_CAPABILITY,
            f"{NAME} adapter does not implement the {grain} grain",
            subject=str(grain),
        )
    target = column
    if timezone.upper() != "UTC":
        target = exp.AtTimeZone(this=column, zone=exp.Literal.string(timezone))
    return exp.func("date_trunc", exp.Literal.string(SUPPORTED_GRAINS[grain]), target)


def aggregate(op: str, operand: exp.Expression | None) -> exp.Expression:
    if op not in SUPPORTED_AGGREGATES:
        raise CompilationError(
            ReasonCode.UNSUPPORTED_OPERATOR,
            f"{NAME} adapter does not implement aggregate {op!r}",
            subject=op,
        )
    match op:
        case "sum":
            return exp.Sum(this=operand)
        case "avg":
            return exp.Avg(this=operand)
        case "min":
            return exp.Min(this=operand)
        case "max":
            return exp.Max(this=operand)
        case "count":
            return exp.Count(this=operand if operand is not None else exp.Star())
        case "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[operand]))
    raise CompilationError(ReasonCode.UNSUPPORTED_OPERATOR, f"unreachable: {op}")
