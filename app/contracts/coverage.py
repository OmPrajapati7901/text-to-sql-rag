"""Requirement coverage matrix.

Every requested concept resolves to exactly one of: a bound object, an approved default, or an
unresolved ambiguity. Missing retrieval evidence is recorded as UNRESOLVED — it is never
recorded as proof that data does not exist.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CoverageState(StrEnum):
    RESOLVED = "resolved"
    APPROVED_DEFAULT = "approved_default"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    UNAUTHORIZED = "unauthorized"


class ConceptCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    concept: str = Field(description="The requested concept, in the user's words")
    kind: str = Field(description="metric | dimension | filter | time | entity | ordering")
    state: CoverageState
    resolved_ref: str | None = None
    evidence_ref: str | None = None
    alternatives: tuple[str, ...] = ()
    note: str | None = None


class CoverageMatrix(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[ConceptCoverage, ...] = ()

    @property
    def is_complete(self) -> bool:
        return all(
            e.state in (CoverageState.RESOLVED, CoverageState.APPROVED_DEFAULT)
            for e in self.entries
        )

    def unresolved(self) -> tuple[ConceptCoverage, ...]:
        return tuple(e for e in self.entries if e.state is CoverageState.UNRESOLVED)

    def ambiguous(self) -> tuple[ConceptCoverage, ...]:
        return tuple(e for e in self.entries if e.state is CoverageState.AMBIGUOUS)

    def with_entry(self, entry: ConceptCoverage) -> CoverageMatrix:
        return CoverageMatrix(entries=(*self.entries, entry))
