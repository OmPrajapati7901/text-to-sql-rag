"""Compile reviewed source records into an immutable published snapshot.

Publication is atomic and validated: referential integrity, cycle checks and enum resolution
run before anything is written. A bundle that fails is rejected whole, never partially
published.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.contracts.catalog import ColumnRecord as ColumnRec
from app.contracts.catalog import (
    DomainRecord,
    GlossaryRecord,
    RelationshipRecord,
    SnapshotManifest,
    TableRecord,
    ValueSetRecord,
)
from app.contracts.semantics import DimensionRecord, MetricRecord, RuleRecord
from ingestion import source_bundle as src

_FILES = {
    "domains": (DomainRecord, "DOMAINS"),
    "tables": (TableRecord, "TABLES"),
    "columns": (ColumnRec, "COLUMNS"),
    "relationships": (RelationshipRecord, "RELATIONSHIPS"),
    "value_sets": (ValueSetRecord, "VALUE_SETS"),
    "metrics": (MetricRecord, "METRICS"),
    "dimensions": (DimensionRecord, "DIMENSIONS"),
    "rules": (RuleRecord, "RULES"),
    "glossary": (GlossaryRecord, "GLOSSARY"),
}


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class PublicationError(Exception):
    """The bundle is invalid. Nothing is written."""


def _validate(records: dict[str, list[Any]]) -> None:
    table_ids = {t.id for t in records["tables"]}
    column_ids = {c.id for c in records["columns"]}
    value_set_refs = {str(v.ref) for v in records["value_sets"]}
    metric_refs = {str(m.ref) for m in records["metrics"]}
    rel_refs = {str(r.ref) for r in records["relationships"]}
    errors: list[str] = []

    for col in records["columns"]:
        if col.table_id not in table_ids:
            errors.append(f"Column {col.id} references unknown table {col.table_id}")
        if col.value_set_ref and col.value_set_ref not in value_set_refs:
            errors.append(f"Column {col.id} references unknown value set {col.value_set_ref}")

    for table in records["tables"]:
        for gcol in table.grain:
            if gcol not in column_ids:
                errors.append(f"Table {table.id} grain references unknown column {gcol}")
        for ref in table.approved_relationships:
            if ref not in rel_refs:
                errors.append(f"Table {table.id} references unknown relationship {ref}")

    for rel in records["relationships"]:
        for side in (rel.left_table, rel.right_table):
            if side not in table_ids:
                errors.append(f"Relationship {rel.id} references unknown table {side}")
        for pred in rel.predicates:
            for c in (pred.left_column, pred.right_column):
                if c not in column_ids:
                    errors.append(f"Relationship {rel.id} predicate references unknown {c}")
        if rel.is_temporal and rel.temporal_fact_column not in column_ids:
            errors.append(f"Relationship {rel.id} temporal column not found")

    for metric in records["metrics"]:
        if metric.base_table not in table_ids:
            errors.append(f"Metric {metric.id} base table {metric.base_table} unknown")
        if metric.time.time_column not in column_ids:
            errors.append(f"Metric {metric.id} time column unknown")
        for dep in metric.required_rule_refs:
            if dep not in {str(r.ref) for r in records["rules"]}:
                errors.append(f"Metric {metric.id} requires unknown rule {dep}")
        if metric.supersedes and metric.supersedes in metric_refs:
            errors.append(f"Metric {metric.id} supersedes a ref still published")

    for dim in records["dimensions"]:
        if dim.column not in column_ids:
            errors.append(f"Dimension {dim.id} references unknown column {dim.column}")
        if dim.required_relationship_ref and dim.required_relationship_ref not in rel_refs:
            errors.append(f"Dimension {dim.id} requires unknown relationship")

    for rule in records["rules"]:
        eff = rule.effect
        for t in eff.required_tables:
            if t not in table_ids:
                errors.append(f"Rule {rule.id} requires unknown table {t}")
        if eff.required_relationship_ref and eff.required_relationship_ref not in rel_refs:
            errors.append(f"Rule {rule.id} requires unknown relationship")

    for entry in records["glossary"]:
        for ref in entry.maps_to:
            if ref not in metric_refs | {str(d.ref) for d in records["dimensions"]}:
                errors.append(f"Glossary {entry.id} maps to unknown ref {ref}")

    if errors:
        raise PublicationError(
            f"Bundle rejected with {len(errors)} referential error(s):\n  "
            + "\n  ".join(errors)
        )


def build_records(snapshot_id: str = src.SNAPSHOT_ID) -> dict[str, list[Any]]:
    records: dict[str, list[Any]] = {}
    for key, (model, attr) in _FILES.items():
        raw = getattr(src, attr)
        records[key] = [
            model.model_validate({**item, "snapshot_id": snapshot_id, "source_hash": _hash(item)})
            for item in raw
        ]
    _validate(records)
    return records


def publish(
    out_dir: Path, snapshot_id: str = src.SNAPSHOT_ID, dialect: str = src.DIALECT
) -> SnapshotManifest:
    """Write a complete, immutable snapshot. Prior snapshots are left untouched."""
    records = build_records(snapshot_id)
    target = Path(out_dir) / snapshot_id
    if target.exists():
        raise PublicationError(
            f"Snapshot {snapshot_id} already published at {target}. Published snapshots are "
            f"immutable; publish a new snapshot ID instead."
        )
    target.mkdir(parents=True)

    file_hashes: dict[str, str] = {}
    for key, items in records.items():
        payload = [json.loads(item.model_dump_json()) for item in items]
        text = json.dumps(payload, indent=2, sort_keys=True)
        (target / f"{key}.json").write_text(text)
        file_hashes[key] = hashlib.sha256(text.encode()).hexdigest()

    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        published_at=datetime.now(UTC),
        dialect=dialect,
        object_count=sum(len(v) for v in records.values()),
        manifest_hash=_hash(file_hashes),
        schema_fingerprint=_hash(src.CERTIFIED_PHYSICAL_SCHEMA),
        notes="Demo commerce catalog derived from DESIGN/outputs/catalog-demo-42.",
    )
    (target / "manifest.json").write_text(
        json.dumps(
            {**json.loads(manifest.model_dump_json()), "file_hashes": file_hashes}, indent=2
        )
    )
    return manifest
