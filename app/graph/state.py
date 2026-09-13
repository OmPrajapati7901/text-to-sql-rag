"""Graph state and runtime context.

State is checkpointed, so it holds references and plain data only: no services, no live
clients, no credentials, and never a TrustedScope. Authorization travels in RequestContext,
which is passed at invocation time and is not persisted.

Every artifact carries its plan_revision, and repair clears downstream artifacts by explicit
overwrite -- never by omitting a key, because an omitted key leaves the stale value in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.contracts.scope import TrustedScope
from app.services import Services
from app.sql.gateway import DuckDbGateway


class GraphState(TypedDict, total=False):
    # -- immutable request envelope ------------------------------------
    request_id: str
    thread_id: str
    question: str
    policy_ref: str
    snapshot_ref: str

    # -- routing and interpretation -------------------------------------
    route: str
    intent: dict[str, Any]
    exact_metric_refs: list[str]
    exact_dimension_refs: list[str]

    # -- retrieval ------------------------------------------------------
    candidates: list[dict[str, Any]]
    retrieval_status: str
    retrieval_findings: list[str]

    # -- planning -------------------------------------------------------
    proposal: dict[str, Any] | None
    plan_summary: dict[str, Any] | None
    sql: str | None
    validation: dict[str, Any] | None

    # -- execution ------------------------------------------------------
    result_columns: list[str]
    result_rows: list[list[Any]]
    job_id: str | None

    # -- control --------------------------------------------------------
    decision: str
    reason_codes: list[str]
    notes: list[str]
    clarification: dict[str, Any] | None
    clarification_answer: dict[str, Any] | None
    clarified_slots: list[str]
    retrieval_rounds: int
    repair_attempts: int
    plan_revision: int
    answer: dict[str, Any] | None


class ArtifactStore:
    """Protected store for large or sensitive artifacts, keyed by plan revision.

    Compiled SQL, bound plans and parameter payloads never enter checkpointed state: a
    parameter value can be sensitive, and a checkpoint is a durable, separately-accessed
    store. State carries a revision number; the artifacts live here.
    """

    def __init__(self) -> None:
        self._by_revision: dict[int, tuple] = {}

    def put(self, revision: int, artifacts: tuple) -> None:
        self._by_revision[revision] = artifacts

    def get(self, revision: int) -> tuple:
        try:
            return self._by_revision[revision]
        except KeyError:
            raise KeyError(
                f"No compiled artifacts for plan revision {revision}. A revision must be "
                f"compiled before it can be executed."
            ) from None

    def discard_from(self, revision: int) -> None:
        """Drop this revision and everything after it, so a repair cannot execute a stale
        artifact by accident."""
        for key in [k for k in self._by_revision if k >= revision]:
            del self._by_revision[key]


@dataclass(frozen=True)
class RequestContext:
    """Trusted runtime context. Supplied by the authenticated API, never by the graph."""

    scope: TrustedScope
    services: Services
    gateway: DuckDbGateway
    retrieval: Any
    understander: Any
    proposer: Any
    artifacts: ArtifactStore = field(default_factory=ArtifactStore)
    deadline_seconds: float = 60.0


# Budgets, per DESIGN §7. These are circuit breakers, not business logic.
MAX_RETRIEVAL_ROUNDS = 2
MAX_REPAIR_ATTEMPTS = 2  # see app.planning.repair


def clear_downstream(revision: int) -> dict[str, Any]:
    """Explicitly overwrite every artifact that a re-plan invalidates."""
    return {
        "proposal": None,
        "plan_summary": None,
        "sql": None,
        "validation": None,
        "result_columns": [],
        "result_rows": [],
        "job_id": None,
        "answer": None,
        "plan_revision": revision,
    }
