"""Exact, authorized metadata access.

This is the source of truth. Search ranking never decides what an object *is* — every
certified object resolves by ID here, independent of retrieval. Authorization is applied on
resolution, not only at discovery.
"""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from app.authorization.policy import PolicyEngine
from app.contracts.catalog import ColumnRecord as ColumnRec
from app.contracts.catalog import (
    DomainRecord,
    GlossaryRecord,
    RelationshipRecord,
    SnapshotManifest,
    TableRecord,
    ValueSetRecord,
)
from app.contracts.errors import AuthorizationError, CatalogError, ReasonCode
from app.contracts.ids import Ref
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantics import DimensionRecord, MetricRecord, RuleRecord

_MODELS: dict[str, Any] = {
    "domains": DomainRecord,
    "tables": TableRecord,
    "columns": ColumnRec,
    "relationships": RelationshipRecord,
    "value_sets": ValueSetRecord,
    "metrics": MetricRecord,
    "dimensions": DimensionRecord,
    "rules": RuleRecord,
    "glossary": GlossaryRecord,
}


class CatalogSnapshot:
    """An immutable loaded bundle. Indexed once at startup."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not (self.path / "manifest.json").exists():
            raise CatalogError(
                ReasonCode.SNAPSHOT_NOT_READY, f"No manifest at {self.path}", subject=str(path)
            )
        raw_manifest = json.loads((self.path / "manifest.json").read_text())
        self.file_hashes: dict[str, str] = raw_manifest.pop("file_hashes", {})
        self.manifest = SnapshotManifest.model_validate(raw_manifest)

        self._records: dict[str, dict[str, Any]] = {}
        for key, model in _MODELS.items():
            items = json.loads((self.path / f"{key}.json").read_text())
            self._records[key] = {i["id"]: model.model_validate(i) for i in items}

    @property
    def snapshot_id(self) -> str:
        return self.manifest.snapshot_id

    @property
    def dialect(self) -> str:
        return self.manifest.dialect

    def all(self, kind: str) -> list[Any]:
        return list(self._records[kind].values())

    def get(self, kind: str, object_id: str) -> Any:
        try:
            return self._records[kind][object_id]
        except KeyError:
            raise CatalogError(
                ReasonCode.OBJECT_NOT_FOUND,
                f"No {kind[:-1]} {object_id!r} in snapshot {self.snapshot_id}",
                subject=object_id,
            ) from None

    def get_pinned(self, kind: str, ref: str | Ref) -> Any:
        """Resolve `finance.revenue@7`. A version mismatch is a hard error: a plan pinned to
        a version must not silently bind to a republished one."""
        parsed = ref if isinstance(ref, Ref) else Ref.parse(str(ref))
        record = self.get(kind, parsed.id)
        if record.version != parsed.version:
            raise CatalogError(
                ReasonCode.STALE_SNAPSHOT,
                f"{parsed.id} is published at version {record.version}, plan pinned "
                f"{parsed.version}",
                subject=str(parsed),
            )
        return record

    @cached_property
    def columns_by_table(self) -> dict[str, list[ColumnRec]]:
        out: dict[str, list[ColumnRec]] = {}
        for col in self._records["columns"].values():
            out.setdefault(col.table_id, []).append(col)
        return out

    @cached_property
    def relationships_by_table(self) -> dict[str, list[RelationshipRecord]]:
        out: dict[str, list[RelationshipRecord]] = {}
        for rel in self._records["relationships"].values():
            out.setdefault(rel.left_table, []).append(rel)
            out.setdefault(rel.right_table, []).append(rel)
        return out


class CatalogService:
    """Authorized access to a pinned snapshot."""

    def __init__(self, snapshot: CatalogSnapshot, policy: PolicyEngine) -> None:
        self.snapshot = snapshot
        self.policy = policy

    def _require_snapshot(self, scope: TrustedScope) -> None:
        if scope.snapshot_id != self.snapshot.snapshot_id:
            raise CatalogError(
                ReasonCode.STALE_SNAPSHOT,
                f"Scope pins {scope.snapshot_id}, service holds {self.snapshot.snapshot_id}",
            )

    def _authorize(self, scope: TrustedScope, action: Action, resource: str) -> None:
        self.policy.check(scope.principal_id, action, resource).require(resource)

    # -- exact resolution -------------------------------------------------

    def get_table(self, table_id: str, scope: TrustedScope) -> TableRecord:
        self._require_snapshot(scope)
        record = self.snapshot.get("tables", table_id)
        self._authorize(scope, Action.DISCOVER, table_id)
        return record

    def get_columns(
        self, table_ids: list[str], scope: TrustedScope, *, action: Action = Action.READ
    ) -> list[ColumnRec]:
        """The authorized column inventory for these tables. Columns the principal may not
        access are omitted — but callers that need a column for a mandatory dependency must
        check explicitly rather than treating absence as optional."""
        self._require_snapshot(scope)
        out: list[ColumnRec] = []
        for table_id in table_ids:
            self._authorize(scope, Action.DISCOVER, table_id)
            for col in self.snapshot.columns_by_table.get(table_id, []):
                if self.policy.check(scope.principal_id, action, col.id).allowed:
                    out.append(col)
        return out

    def get_column(self, column_id: str, scope: TrustedScope) -> ColumnRec:
        self._require_snapshot(scope)
        record = self.snapshot.get("columns", column_id)
        self._authorize(scope, Action.DISCOVER, column_id)
        return record

    def require_columns_usable(
        self, column_ids: frozenset[str], scope: TrustedScope, action: Action
    ) -> None:
        """Fail closed on the whole set. Used for mandatory dependencies, where dropping an
        inaccessible column would silently change meaning."""
        denied = self.policy.check_all(scope.principal_id, action, column_ids)
        if denied:
            raise AuthorizationError(
                ReasonCode.NOT_AUTHORIZED,
                f"Principal may not {action} {len(denied)} required column(s): {', '.join(denied)}",
                subject=denied[0],
            )

    def get_metric(self, ref: str, scope: TrustedScope) -> MetricRecord:
        self._require_snapshot(scope)
        record = self.snapshot.get_pinned("metrics", ref)
        self._authorize(scope, Action.DISCOVER, record.id)
        self._authorize(scope, Action.EXECUTE_METRIC, record.id)
        return record

    def get_metric_contract(self, ref: str, scope: TrustedScope) -> MetricRecord:
        """Public contract only — discovery permission, without execute. Used so the planner
        can describe a metric it may not run."""
        self._require_snapshot(scope)
        record = self.snapshot.get_pinned("metrics", ref)
        self._authorize(scope, Action.DISCOVER, record.id)
        return record

    def get_dimension(self, ref: str, scope: TrustedScope) -> DimensionRecord:
        self._require_snapshot(scope)
        record = self.snapshot.get_pinned("dimensions", ref)
        self._authorize(scope, Action.DISCOVER, record.id)
        return record

    def get_relationship(self, ref: str, scope: TrustedScope) -> RelationshipRecord:
        self._require_snapshot(scope)
        record = self.snapshot.get_pinned("relationships", ref)
        self._authorize(scope, Action.DISCOVER, record.left_table)
        self._authorize(scope, Action.DISCOVER, record.right_table)
        return record

    def get_rule(self, ref: str) -> RuleRecord:
        """Rules are not discovery-filtered: a mandatory rule applies whether or not the
        principal can see its definition. Its *effect* is authorized when bound."""
        return self.snapshot.get_pinned("rules", ref)

    def all_rules(self) -> list[RuleRecord]:
        return self.snapshot.all("rules")

    def resolve_enum(self, value_set_ref: str, member: str) -> str:
        vs: ValueSetRecord = self.snapshot.get_pinned("value_sets", value_set_ref)
        try:
            return vs.members[member]
        except KeyError:
            raise CatalogError(
                ReasonCode.OBJECT_NOT_FOUND,
                f"{member!r} is not a member of {value_set_ref}",
                subject=value_set_ref,
            ) from None

    def lookup_term(self, text: str, scope: TrustedScope) -> list[GlossaryRecord]:
        """Exact glossary match. Resolves straightforward cases in code before any model."""
        self._require_snapshot(scope)
        needle = text.strip().lower()
        hits = []
        for entry in self.snapshot.all("glossary"):
            names = {entry.term.lower(), *(s.lower() for s in entry.synonyms)}
            if (
                needle in names
                and self.policy.check(scope.principal_id, Action.DISCOVER, entry.id).allowed
            ):
                hits.append(entry)
        return hits

    def certified_tables(self) -> list[TableRecord]:
        return [t for t in self.snapshot.all("tables") if t.is_certified]

    def queryable_tables(self, scope: TrustedScope) -> list[TableRecord]:
        """Certified tables the principal may both discover and read."""

        self._require_snapshot(scope)
        return [
            table
            for table in self.certified_tables()
            if self.policy.check(scope.principal_id, Action.DISCOVER, table.id).allowed
            and self.policy.check(scope.principal_id, Action.READ, table.id).allowed
        ]

    def executable_metrics(self, scope: TrustedScope) -> list[MetricRecord]:
        """Published metrics the principal may discover and execute."""

        self._require_snapshot(scope)
        return [
            metric
            for metric in self.snapshot.all("metrics")
            if self.policy.check(scope.principal_id, Action.DISCOVER, metric.id).allowed
            and self.policy.check(scope.principal_id, Action.EXECUTE_METRIC, metric.id).allowed
        ]
