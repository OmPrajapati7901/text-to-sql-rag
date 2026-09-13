"""Bounded plan repair.

The repairable set here is deliberately small, and that is the design rather than an omission.

DESIGN §18 forbids a repair from changing the requested population, metric version, time
interval, tenant scope, exactness or mandatory rules. In this system almost every failure
touches one of those: an unsupported grain, an unsupported dimension, or a missing metric all
change what the user asked for. Silently "fixing" them is precisely the substitution §45
prohibits, and an answer that quietly measures something else is worse than no answer.

So semantic gaps route to clarification or an explicit stop, and repair handles only changes
that cannot alter a number: presentation-level defects and compiler-level mistakes.

Every repair is re-bound, re-compiled and re-validated from scratch. A repaired plan gets no
shortcut through the gates.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.errors import Finding, GovernedError, ReasonCode
from app.contracts.semantic_plan import SemanticRequest

MAX_REPAIR_ATTEMPTS = 2

# Only these can be repaired. Anything else clarifies or stops.
REPAIRABLE_CODES = frozenset({
    ReasonCode.VALIDATION_FAILED,
    ReasonCode.COMPILER_DEFECT,
})

# A repair may never alter these. They define what was asked.
LOCKED_FIELDS = ("metric_ids", "time_range", "filters", "attribution")


@dataclass(frozen=True)
class RepairOutcome:
    request: SemanticRequest | None
    description: str
    repaired: bool = False


def guard(original: SemanticRequest, repaired: SemanticRequest) -> None:
    """Reject a diff that changes the meaning of the request.

    This runs on every repair, so a future repair rule cannot quietly widen its own remit.
    """
    for field in LOCKED_FIELDS:
        before, after = getattr(original, field), getattr(repaired, field)
        if before != after:
            raise GovernedError(
                ReasonCode.VALIDATION_FAILED,
                f"Repair attempted to change {field!r}, which is locked. A repair may not "
                f"alter the requested population, metric, period or filters.",
                subject=field,
            )
    if repaired.limit is not None and (original.limit is None or repaired.limit > original.limit):
        raise GovernedError(
            ReasonCode.VALIDATION_FAILED,
            "Repair attempted to widen the row limit, which changes what is returned.",
            subject="limit",
        )


def propose_repair(
    request: SemanticRequest,
    findings: list[Finding],
    *,
    result_columns: tuple[str, ...] = (),
) -> RepairOutcome:
    """Deterministic repair. Returns an unrepaired outcome when nothing safe applies."""
    codes = {f.code for f in findings}
    if not codes & REPAIRABLE_CODES:
        return RepairOutcome(
            request=None,
            description=(
                "No safe repair exists for "
                f"{', '.join(sorted(c.value for c in codes))}. A compiler defect, denied "
                f"operation or missing approved relationship is not an invitation to change "
                f"the question."
            ),
        )

    repaired = request
    notes: list[str] = []

    # Presentation only: a sort key that names nothing in the output contract cannot be
    # honoured, and dropping it changes no number.
    if result_columns:
        valid = tuple(k for k in repaired.order_by if k.field in result_columns)
        if len(valid) != len(repaired.order_by):
            dropped = [k.field for k in repaired.order_by if k.field not in result_columns]
            repaired = repaired.model_copy(update={"order_by": valid})
            notes.append(
                f"Dropped sort key(s) {', '.join(dropped)} that name no output column."
            )

    if not notes:
        return RepairOutcome(
            request=None,
            description=(
                "The failure is real but no meaning-preserving repair applies. Blocking "
                "rather than altering the request."
            ),
        )

    guard(request, repaired)
    return RepairOutcome(request=repaired, description=" ".join(notes), repaired=True)
