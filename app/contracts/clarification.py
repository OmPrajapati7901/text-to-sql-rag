"""Clarification contracts.

A clarification asks for *business meaning*, never for a table name or a SQL detail. It is
raised only when alternatives would change the answer and no authoritative default resolves
them (DESIGN §21).

The resumed answer is validated against the options that were offered. A resume payload is
untrusted input: without that check, resuming could inject an arbitrary metric ID straight
past retrieval and authorization.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.errors import GovernedError, ReasonCode


class ClarificationOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str = Field(description="The ID or enum the binder will apply if chosen")
    label: str = Field(description="Business meaning, in the user's vocabulary")
    effect: str = Field(default="", description="What changes in the answer if chosen")


class ClarificationRequest(BaseModel):
    """The interrupt payload. Contains only objects the principal may already discover."""

    model_config = ConfigDict(frozen=True)

    slot: str = Field(description="metric | dimension | time_grain | time_range")
    question: str
    options: tuple[ClarificationOption, ...] = Field(min_length=2)
    reason: str = Field(default="", description="Why this materially changes the answer")

    def to_text(self) -> str:
        lines = [self.question]
        for i, option in enumerate(self.options, start=1):
            suffix = f" — {option.effect}" if option.effect else ""
            lines.append(f"  {i}. {option.label}{suffix}")
        return "\n".join(lines)


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: str
    value: str


def validate_answer(payload: object, request: ClarificationRequest) -> ClarificationAnswer:
    """Accept only a value that was actually offered.

    The payload arrives from a resume call, so it is untrusted. Matching it against the
    offered options keeps clarification from becoming a path that bypasses retrieval scoping
    and authorization.
    """
    if isinstance(payload, ClarificationAnswer):
        answer = payload
    elif isinstance(payload, dict):
        try:
            answer = ClarificationAnswer.model_validate(payload)
        except Exception as exc:
            raise GovernedError(
                ReasonCode.MATERIAL_AMBIGUITY,
                f"Clarification answer was not understood: {exc}",
            ) from None
    elif isinstance(payload, str):
        # A bare string is accepted as an option label, value, or 1-based index.
        answer = _match_freeform(payload, request)
    else:
        raise GovernedError(
            ReasonCode.MATERIAL_AMBIGUITY,
            f"Clarification answer must be an object or a choice, got {type(payload).__name__}",
        )

    if answer.slot != request.slot:
        raise GovernedError(
            ReasonCode.MATERIAL_AMBIGUITY,
            f"Answer addresses slot {answer.slot!r}, the question asked about "
            f"{request.slot!r}",
        )
    permitted = {option.value for option in request.options}
    if answer.value not in permitted:
        raise GovernedError(
            ReasonCode.NOT_AUTHORIZED,
            f"{answer.value!r} was not one of the offered choices. A clarification cannot "
            f"introduce an object that was never offered.",
            subject=answer.value,
        )
    return answer


def _match_freeform(text: str, request: ClarificationRequest) -> ClarificationAnswer:
    needle = text.strip().lower()
    if needle.isdigit():
        index = int(needle) - 1
        if 0 <= index < len(request.options):
            return ClarificationAnswer(slot=request.slot, value=request.options[index].value)
    for option in request.options:
        if needle in (option.value.lower(), option.label.lower()):
            return ClarificationAnswer(slot=request.slot, value=option.value)
    raise GovernedError(
        ReasonCode.MATERIAL_AMBIGUITY,
        f"{text!r} does not match any offered choice: "
        + ", ".join(o.label for o in request.options),
    )
