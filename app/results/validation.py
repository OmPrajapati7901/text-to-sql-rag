"""Result contract validation.

Hard contract violations block. Anomalies warn. An unusually low number or an empty result is
an anomaly, not proof of an incorrect query — and a filter is never loosened to produce rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.contracts.errors import Finding, ReasonCode, Severity
from app.contracts.semantic_plan import BoundSemanticPlan
from app.sql.gateway import QueryResult


@dataclass
class ResultReport:
    findings: list[Finding]
    row_count: int
    is_empty_but_valid: bool = False

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.BLOCK for f in self.findings)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]


def validate_result(result: QueryResult, plan: BoundSemanticPlan) -> ResultReport:
    contract = plan.result_contract
    findings: list[Finding] = []

    # 1. Shape: exact columns, in contract order.
    if result.columns != contract.columns:
        findings.append(Finding(
            code=ReasonCode.RESULT_CONTRACT_BREACH,
            message=f"Result columns {result.columns} do not match the contract "
                    f"{contract.columns}"))
        return ResultReport(findings=findings, row_count=result.row_count)

    # 2. Completeness: a truncated page must never be presented as a total.
    if result.truncated:
        findings.append(Finding(
            code=ReasonCode.RESULT_CONTRACT_BREACH,
            message=f"Result was truncated at {result.row_count} rows; a partial result "
                    f"cannot be released as a complete answer"))

    # 3. Output grain uniqueness -- duplicate keys mean the join multiplied rows.
    if contract.unique_keys:
        key_index = [contract.columns.index(k) for k in contract.unique_keys]
        seen: set[tuple] = set()
        duplicates: list[tuple] = []
        for row in result.rows:
            key = tuple(row[i] for i in key_index)
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        if duplicates:
            findings.append(Finding(
                code=ReasonCode.RESULT_CONTRACT_BREACH,
                message=f"Output grain {contract.unique_keys} is not unique; "
                        f"{len(duplicates)} duplicate key(s), e.g. {duplicates[0]}. This "
                        f"indicates join fan-out, not a presentation issue."))

    # 4. Empty result: legitimate, and explicitly distinguished from missing evidence.
    is_empty_valid = False
    if result.row_count == 0:
        is_empty_valid = True
        findings.append(Finding(
            code=ReasonCode.RESULT_CONTRACT_BREACH,
            severity=Severity.INFO,
            message="No rows matched the authorized, fully-bound query. This is a valid "
                    "empty result for the requested population, not evidence that the "
                    "underlying data is absent."))

    # 5. Measure-specific checks against the metric contract.
    for metric in plan.metrics:
        if metric.output_alias not in contract.columns:
            continue
        idx = contract.columns.index(metric.output_alias)
        values = [row[idx] for row in result.rows]
        nulls = sum(1 for v in values if v is None)
        if nulls:
            findings.append(Finding(
                code=ReasonCode.RESULT_CONTRACT_BREACH,
                severity=Severity.WARN,
                message=f"{nulls} null value(s) in measure {metric.output_alias!r}",
                subject=metric.output_alias))
        for value in values:
            if isinstance(value, (int, float, Decimal)) and math.isnan(value):
                findings.append(Finding(
                    code=ReasonCode.RESULT_CONTRACT_BREACH,
                    message=f"Measure {metric.output_alias!r} contains NaN",
                    subject=metric.output_alias))
                break

    # 6. Dimension nulls can indicate an unmatched join under an inner-join contract.
    for dim in plan.dimensions:
        if dim.output_alias not in contract.columns:
            continue
        idx = contract.columns.index(dim.output_alias)
        if any(row[idx] is None for row in result.rows):
            findings.append(Finding(
                code=ReasonCode.RESULT_CONTRACT_BREACH,
                severity=Severity.WARN,
                message=f"Dimension {dim.output_alias!r} contains nulls; check the join's "
                        f"unmatched-row policy",
                subject=dim.output_alias))

    return ResultReport(
        findings=findings, row_count=result.row_count, is_empty_but_valid=is_empty_valid
    )
