"""Structured error and reason codes.

Every expected failure follows an explicit path. Unexpected exceptions are converted by the
API into a safe failed request, never continued as success.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ReasonCode(StrEnum):
    """Enumerated outcomes. Routing returns these, never free text."""

    # Retrieval / provider
    POLICY_UNREPRESENTABLE = "POLICY_UNREPRESENTABLE"
    SNAPSHOT_NOT_READY = "SNAPSHOT_NOT_READY"
    PARTIAL_RETRIEVAL = "PARTIAL_RETRIEVAL"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

    # Authorization
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    DISCOVERY_DENIED = "DISCOVERY_DENIED"
    POLICY_EPOCH_CHANGED = "POLICY_EPOCH_CHANGED"

    # Catalog
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
    SNAPSHOT_DRIFT = "SNAPSHOT_DRIFT"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"

    # Semantics
    METRIC_NOT_FOUND = "METRIC_NOT_FOUND"
    RULE_CONFLICT = "RULE_CONFLICT"
    RULE_CLOSURE_NOT_CONVERGED = "RULE_CLOSURE_NOT_CONVERGED"
    MANDATORY_RULE_UNRESOLVED = "MANDATORY_RULE_UNRESOLVED"

    # Relationships
    NO_APPROVED_PATH = "NO_APPROVED_PATH"
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"
    PROHIBITED_PATH = "PROHIBITED_PATH"
    GRAIN_UNPROVEN = "GRAIN_UNPROVEN"
    FAN_OUT_UNPROVEN = "FAN_OUT_UNPROVEN"

    # Planning / compilation
    UNRESOLVED_REFERENCE = "UNRESOLVED_REFERENCE"
    UNSUPPORTED_OPERATOR = "UNSUPPORTED_OPERATOR"
    COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"
    MATERIAL_AMBIGUITY = "MATERIAL_AMBIGUITY"
    COMPILER_DEFECT = "COMPILER_DEFECT"

    # Validation
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNSAFE_QUERY = "UNSAFE_QUERY"
    COST_EXCEEDED = "COST_EXCEEDED"

    # Execution
    ADMISSION_TICKET_INVALID = "ADMISSION_TICKET_INVALID"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RESULT_CONTRACT_BREACH = "RESULT_CONTRACT_BREACH"

    # Budgets
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


class Finding(BaseModel):
    """One structured diagnostic. Carries no private chain-of-thought."""

    model_config = ConfigDict(frozen=True)

    code: ReasonCode
    severity: Severity = Severity.BLOCK
    message: str
    subject: str | None = Field(default=None, description="Object/field the finding concerns")
    evidence_ref: str | None = None

    def __str__(self) -> str:  # pragma: no cover - display helper
        where = f" [{self.subject}]" if self.subject else ""
        return f"{self.severity.upper()} {self.code}{where}: {self.message}"


class GovernedError(Exception):
    """Base for expected, explicitly-routed failures."""

    def __init__(self, code: ReasonCode, message: str, subject: str | None = None) -> None:
        self.finding = Finding(code=code, message=message, subject=subject)
        super().__init__(str(self.finding))


class AuthorizationError(GovernedError):
    """Fail closed. Never repaired by changing privilege."""


class CatalogError(GovernedError):
    """Exact metadata resolution failed. No fuzzy substitution."""


class RetrievalError(GovernedError):
    """Discovery failed or could not be faithfully scoped."""


class CompilationError(GovernedError):
    """The plan could not be lowered to supported SQL."""


class ValidationError(GovernedError):
    """A hard gate failed. Not overridable by confidence."""
