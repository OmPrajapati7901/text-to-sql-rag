"""Deterministic answer rendering.

Numbers come straight from verified result cells. Arithmetic, rounding and units are produced
by code, never by a model. A narrative is optional and every claim in it must be grounded.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.semantic_plan import BoundSemanticPlan
from app.sql.gateway import QueryResult


class AnswerEnvelope(BaseModel):
    """What the user receives. Every field is evidence-backed.

    A Pydantic model rather than a dataclass so it round-trips through checkpointed graph
    state without a bespoke serializer.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    unit: str | None = None
    metric_versions: dict[str, str] = Field(default_factory=dict)
    applied_rules: tuple[str, ...] = ()
    join_paths: tuple[str, ...] = ()
    time_interpretation: str | None = None
    evidence_status: str = "verified"
    notes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_text(self) -> str:
        lines: list[str] = []
        if self.status != "answered":
            lines.append(f"[{self.status.upper()}]")
            for note in self.notes:
                lines.append(f"  {note}")
            if self.reason_codes:
                lines.append(f"  reason codes: {', '.join(self.reason_codes)}")
            return "\n".join(lines)

        if not self.columns:
            # A definition or metadata answer: prose, not a table.
            lines.extend(self.notes)
            lines.append("")
            lines.append("Provenance")
            lines.append(f"  evidence      {self.evidence_status}")
            return "\n".join(lines)

        if not self.rows:
            lines.append("No rows matched the requested population.")
        else:
            widths = [
                max(len(str(c)), *(len(_fmt(r[i])) for r in self.rows))
                for i, c in enumerate(self.columns)
            ]
            lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(self.columns)))
            lines.append("  ".join("-" * w for w in widths))
            for row in self.rows:
                lines.append("  ".join(_fmt(v).ljust(widths[i]) for i, v in enumerate(row)))

        lines.append("")
        lines.append("Provenance")
        for name, version in self.metric_versions.items():
            lines.append(f"  metric        {name} ({version})")
        if self.time_interpretation:
            lines.append(f"  time          {self.time_interpretation}")
        if self.applied_rules:
            lines.append(f"  rules applied {', '.join(self.applied_rules)}")
        if self.join_paths:
            lines.append(f"  join path     {', '.join(self.join_paths)}")
        lines.append(f"  evidence      {self.evidence_status}")
        for note in self.notes:
            lines.append(f"  note          {note}")
        return "\n".join(lines)

    def to_markdown(self, *, max_rows: int = 100) -> str:
        """Render a compact Chainlit-friendly answer without changing CLI output."""

        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")

        lines = [f"**Status:** `{_md_text(self.status)}`"]
        if self.status != "answered":
            if self.notes:
                lines.extend(("", *(_md_text(note) for note in self.notes)))
            if self.reason_codes:
                lines.extend(
                    (
                        "",
                        "**Reason codes:** "
                        + ", ".join(f"`{_md_text(code)}`" for code in self.reason_codes),
                    )
                )
            lines.extend(("", f"**Evidence:** {_md_text(self.evidence_status)}"))
            return "\n".join(lines)

        if not self.columns:
            if self.notes:
                lines.extend(("", *(_md_text(note) for note in self.notes)))
        elif not self.rows:
            lines.extend(("", "No rows matched the requested population."))
        else:
            shown = self.rows[:max_rows]
            lines.extend(
                (
                    "",
                    "| " + " | ".join(_md_cell(column) for column in self.columns) + " |",
                    "| " + " | ".join("---" for _ in self.columns) + " |",
                )
            )
            for row in shown:
                lines.append("| " + " | ".join(_md_cell(value) for value in row) + " |")
            if len(self.rows) > len(shown):
                lines.extend(
                    (
                        "",
                        f"_Showing {len(shown)} of {len(self.rows)} rows. "
                        "Download the CSV for the complete result._",
                    )
                )

        lines.extend(("", "### Provenance", ""))
        if self.unit:
            lines.append(f"- Unit: {_md_text(self.unit)}")
        for name, version in self.metric_versions.items():
            lines.append(f"- Metric: {_md_text(name)} ({_md_text(version)})")
        if self.time_interpretation:
            lines.append(f"- Time: {_md_text(self.time_interpretation)}")
        if self.applied_rules:
            lines.append(
                "- Rules applied: " + ", ".join(_md_text(rule) for rule in self.applied_rules)
            )
        if self.join_paths:
            lines.append("- Join path: " + ", ".join(_md_text(path) for path in self.join_paths))
        lines.append(f"- Evidence: {_md_text(self.evidence_status)}")
        for note in self.notes:
            lines.append(f"- Note: {_md_text(note)}")
        if self.reason_codes:
            lines.append(
                "- Reason codes: " + ", ".join(f"`{_md_text(code)}`" for code in self.reason_codes)
            )
        return "\n".join(lines)

    def to_csv_bytes(self) -> bytes:
        """Return the full tabular result as UTF-8 CSV, entirely in memory."""

        if not self.rows:
            return b""
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(self.columns)
        writer.writerows(tuple(_csv_fmt(value) for value in row) for row in self.rows)
        return stream.getvalue().encode("utf-8")

    @property
    def is_prose(self) -> bool:
        return self.status == "answered" and not self.columns


_ISO_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(?:\.0+)?(?:Z|[+-]\d{2}:\d{2})$")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, str):
        # Values round-trip through JSON in checkpointed state, so a time bucket arrives as
        # an ISO string. Render the date it represents, not the serialization.
        match = _ISO_MIDNIGHT.match(value)
        if match:
            return match.group(1)
    return str(value)


def _md_cell(value: Any) -> str:
    return _md_text(_fmt(value)).replace("\n", "<br>")


def _md_text(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _csv_fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def render(
    result: QueryResult, plan: BoundSemanticPlan, *, notes: tuple[str, ...] = ()
) -> AnswerEnvelope:
    time_note = None
    if plan.time:
        rng = plan.time.range
        grain = f", by {plan.time.grain.value}" if plan.time.grain else ""
        time_note = (
            f"[{rng.start.isoformat()}, {rng.end_exclusive.isoformat()}) "
            f"{rng.timezone}{grain} — end exclusive"
        )
    return AnswerEnvelope(
        status="answered",
        columns=result.columns,
        rows=result.rows,
        unit=plan.result_contract.unit,
        metric_versions={m.ref.split("@")[0]: f"v{m.ref.split('@')[1]}" for m in plan.metrics},
        applied_rules=plan.rule_refs,
        join_paths=plan.relationship_refs,
        time_interpretation=time_note,
        evidence_status="verified",
        notes=notes,
    )


def refusal(status: str, notes: tuple[str, ...], reason_codes: tuple[str, ...]) -> AnswerEnvelope:
    """An explicit terminal response. Never a partial answer dressed as a complete one."""
    return AnswerEnvelope(
        status=status, evidence_status="unavailable", notes=notes, reason_codes=reason_codes
    )
