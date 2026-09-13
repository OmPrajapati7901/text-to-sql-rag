"""Execution gateway — the only component holding query credentials.

An admission ticket binds the exact canonical SQL, parameter payload, policy epoch, snapshot
and limits. The gateway verifies the ticket against the query actually submitted, so validated
SQL cannot be swapped afterwards.

The model never reaches this module.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from app.contracts.errors import GovernedError, ReasonCode
from app.contracts.scope import TrustedScope
from app.contracts.semantic_plan import CompiledQuery

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ROWS = 10_000


def _canonical_hash(sql: str, values: tuple[Any, ...]) -> str:
    payload = json.dumps(
        {"sql": " ".join(sql.split()), "values": [str(v) for v in values]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class AdmissionTicket:
    ticket_id: str
    query_hash: str
    plan_id: str
    plan_revision: int
    principal_id: str
    tenant_id: str
    policy_epoch: int
    snapshot_id: str
    expires_at: datetime
    max_rows: int = DEFAULT_MAX_ROWS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def verify(self, compiled: CompiledQuery, scope: TrustedScope) -> None:
        now = datetime.now(UTC)
        if now > self.expires_at:
            raise GovernedError(
                ReasonCode.ADMISSION_TICKET_INVALID, "Admission ticket has expired"
            )
        actual = _canonical_hash(compiled.sql, compiled.parameter_values)
        if actual != self.query_hash:
            raise GovernedError(
                ReasonCode.ADMISSION_TICKET_INVALID,
                "Submitted query does not match the admitted query. Validated SQL cannot be "
                "substituted after admission.",
            )
        if scope.policy_epoch != self.policy_epoch:
            raise GovernedError(
                ReasonCode.POLICY_EPOCH_CHANGED,
                f"Ticket issued under policy epoch {self.policy_epoch}, current is "
                f"{scope.policy_epoch}",
            )
        if scope.principal_id != self.principal_id or scope.tenant_id != self.tenant_id:
            raise GovernedError(
                ReasonCode.ADMISSION_TICKET_INVALID, "Ticket principal/tenant mismatch"
            )


@dataclass
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool
    duration_ms: int
    job_id: str

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


@dataclass
class ExecutionLedger:
    """Records every submission so an uncertain outcome can be reconciled rather than
    blindly resubmitted."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def open(self, ticket: AdmissionTicket) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        self.entries[job_id] = {
            "ticket_id": ticket.ticket_id,
            "query_hash": ticket.query_hash,
            "state": "submitted",
            "submitted_at": datetime.now(UTC).isoformat(),
        }
        return job_id

    def close(self, job_id: str, state: str, detail: str | None = None) -> None:
        if job_id in self.entries:
            self.entries[job_id].update(
                state=state, detail=detail, closed_at=datetime.now(UTC).isoformat()
            )

    def find_open(self, query_hash: str) -> str | None:
        for job_id, entry in self.entries.items():
            if entry["query_hash"] == query_hash and entry["state"] == "submitted":
                return job_id
        return None


class DuckDbGateway:
    """Read-only execution against an ATTACHed warehouse.

    DuckDB has no row-level security, so tenant isolation here is enforced by the compiler's
    bound predicate plus a read-only attachment. That is defense in depth, NOT the independent
    database boundary §17 assumes -- see DEVIATIONS.md.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.ledger = ExecutionLedger()

    def issue_ticket(
        self,
        compiled: CompiledQuery,
        scope: TrustedScope,
        *,
        ttl_seconds: int = 120,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> AdmissionTicket:
        return AdmissionTicket(
            ticket_id=f"tkt_{uuid.uuid4().hex[:12]}",
            query_hash=_canonical_hash(compiled.sql, compiled.parameter_values),
            plan_id=compiled.plan_id,
            plan_revision=compiled.plan_revision,
            principal_id=scope.principal_id,
            tenant_id=scope.tenant_id,
            policy_epoch=scope.policy_epoch,
            snapshot_id=scope.snapshot_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )

    def _connect(self) -> duckdb.DuckDBPyConnection:
        # An in-memory session with the warehouse ATTACHed read-only. The logical catalog name
        # is `warehouse`, which is what the compiler emits.
        con = duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        con.execute(f"ATTACH '{self.database_path}' AS warehouse (READ_ONLY)")
        return con

    def preflight(self, compiled: CompiledQuery, ticket: AdmissionTicket) -> dict[str, Any]:
        """Non-executing plan check.

        EXPLAIN only. EXPLAIN ANALYZE actually runs the query and is never a safety check.
        """
        con = self._connect()
        try:
            plan_rows = con.execute(
                f"EXPLAIN {compiled.sql}", list(compiled.parameter_values)
            ).fetchall()
            plan_text = "\n".join(str(r[-1]) for r in plan_rows)
            return {
                "ok": True,
                "plan": plan_text,
                "scans": plan_text.count("SCAN"),
            }
        except Exception as exc:
            raise GovernedError(
                ReasonCode.VALIDATION_FAILED, f"Preflight rejected the query: {exc}"
            ) from exc
        finally:
            con.close()

    def run_attestation(self, attestation) -> tuple[int, int, int]:
        """Run one uniqueness/coverage diagnostic. Returns (facts, overlapping, unmatched).

        This is the single targeted diagnostic query permitted before execution; it is
        charged to the same budget and runs under the same read-only attachment.
        """
        con = self._connect()
        try:
            row = con.execute(
                attestation.sql, list(attestation.parameter_values)
            ).fetchone()
            return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))
        except Exception as exc:
            raise GovernedError(
                ReasonCode.VALIDATION_FAILED,
                f"Uniqueness attestation could not complete: {exc}. An unproven join "
                f"contract blocks rather than proceeding.",
            ) from exc
        finally:
            con.close()

    def execute(
        self, compiled: CompiledQuery, ticket: AdmissionTicket, scope: TrustedScope
    ) -> QueryResult:
        ticket.verify(compiled, scope)

        existing = self.ledger.find_open(ticket.query_hash)
        if existing:
            raise GovernedError(
                ReasonCode.EXECUTION_FAILED,
                f"An identical submission ({existing}) is still open. Reconcile it rather "
                f"than duplicating work.",
            )

        job_id = self.ledger.open(ticket)
        con = self._connect()
        started = time.perf_counter()
        try:
            cursor = con.execute(compiled.sql, list(compiled.parameter_values))
            columns = tuple(d[0] for d in cursor.description)
            # Fetch one more than the cap so truncation is detected rather than guessed.
            rows = cursor.fetchmany(ticket.max_rows + 1)
            truncated = len(rows) > ticket.max_rows
            rows = rows[: ticket.max_rows]
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.ledger.close(job_id, "completed")
            return QueryResult(
                columns=columns,
                rows=tuple(tuple(r) for r in rows),
                row_count=len(rows),
                truncated=truncated,
                duration_ms=duration_ms,
                job_id=job_id,
            )
        except Exception as exc:
            self.ledger.close(job_id, "failed", str(exc))
            raise GovernedError(
                ReasonCode.EXECUTION_FAILED, f"Execution failed: {exc}"
            ) from exc
        finally:
            con.close()
