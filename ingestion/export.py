"""Export discovery cards from a published snapshot.

Source records and search projections are separate artifacts. A card is a concise, atomic,
embeddable description of one object; it is a *discovery aid*, never an executable definition.
The executable definition is always fetched from the catalog by ID.

Two rules the export enforces:

* Every column card carries its parent `table_id`, so column search can be scoped to a table.
* A restricted column's description never appears inside a more widely visible table card.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.catalog.service import CatalogSnapshot


@dataclass
class Card:
    chunk_id: str
    object_id: str
    object_type: str
    title: str
    content: str
    domain: str
    access_scope: str
    snapshot_id: str
    source_version: str
    source_hash: str
    table_id: str | None = None
    aliases: tuple[str, ...] = ()
    publication_status: str = "approved"
    embedding: list[float] | None = field(default=None, repr=False)

    def to_document(self) -> dict[str, Any]:
        doc = {
            "chunk_id": self.chunk_id,
            "object_id": self.object_id,
            "object_type": self.object_type,
            "table_id": self.table_id,
            "domain": self.domain,
            "access_scope": self.access_scope,
            "snapshot_id": self.snapshot_id,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "publication_status": self.publication_status,
            "title": self.title,
            "aliases": list(self.aliases),
            "content": self.content,
            "source_ref": f"catalog:{self.object_id}@{self.source_version}",
        }
        if self.embedding is not None:
            doc["embedding"] = self.embedding
        return doc


def _chunk_id(snapshot_id: str, object_id: str, version: str) -> str:
    """Unique per tenant-visible snapshot, object version and chunk, so republishing a
    snapshot cannot overwrite a record a pinned request still depends on."""
    raw = f"{snapshot_id}:{object_id}:{version}"
    return f"{object_id}#{hashlib.sha256(raw.encode()).hexdigest()[:10]}"


def export_cards(snapshot: CatalogSnapshot) -> list[Card]:
    cards: list[Card] = []
    snap = snapshot.snapshot_id

    def add(record, object_type: str, title: str, lines: list[str],
            aliases: tuple[str, ...] = (), table_id: str | None = None) -> None:
        cards.append(Card(
            chunk_id=_chunk_id(snap, record.id, record.source_version),
            object_id=record.id, object_type=object_type, title=title,
            content="\n".join(line for line in lines if line), domain=record.domain,
            access_scope=record.access_scope, snapshot_id=snap,
            source_version=record.source_version, source_hash=record.source_hash,
            table_id=table_id, aliases=aliases,
            publication_status=record.publication_status.value,
        ))

    for d in snapshot.all("domains"):
        add(d, "domain", d.name, [d.description, f"Reporting timezone: {d.default_timezone}."])

    for t in snapshot.all("tables"):
        # Only the table's own prose. Column descriptions stay in column cards, which carry
        # their own access scope.
        add(t, "table", t.name, [
            f"Qualified table: {t.physical}",
            t.business_description or t.description,
            f"Grain: one row per {', '.join(c.rsplit('.', 1)[1] for c in t.grain)}.",
            f"Certified for execution: {'yes' if t.is_certified else 'no'}.",
            f"Associated metrics: {', '.join(t.metric_refs)}." if t.metric_refs else "",
            f"Do not use for: {', '.join(t.prohibited_usages)}." if t.prohibited_usages else "",
        ], aliases=t.synonyms)

    for c in snapshot.all("columns"):
        add(c, "column", c.name, [
            f"Column: {c.id}",
            f"Type: {c.data_type}{'' if c.nullable else ', NOT NULL'}."
            + (f" Unit: {c.unit}." if c.unit else ""),
            c.description,
        ], aliases=c.synonyms, table_id=c.table_id)

    for m in snapshot.all("metrics"):
        add(m, "metric", m.name, [
            f"Metric ID: {m.id}. Version: {m.version}.",
            m.description,
            f"Unit: {m.unit}." if m.unit else "",
            f"Time field: {m.time.time_column}. Timezone: {m.time.timezone}. "
            f"Grains: {', '.join(g.value for g in m.time.grains)}.",
            f"Supported dimensions: {', '.join(m.allowed_dimensions)}."
            if m.allowed_dimensions else "",
            f"Required rules: {', '.join(m.required_rule_refs)}."
            if m.required_rule_refs else "",
            f"Canonical reference: registry:{m.id}@{m.version}. "
            f"Fetch the executable definition from the registry.",
        ], aliases=m.aliases)

    for d in snapshot.all("dimensions"):
        add(d, "dimension", d.name, [
            f"Dimension ID: {d.id}. Version: {d.version}.",
            d.description,
            f"Attribute column: {d.column}.",
            f"Temporal binding: {d.temporal_binding}." if d.temporal_binding else "",
        ], aliases=d.aliases)

    for r in snapshot.all("rules"):
        add(r, "rule", r.name, [
            f"Rule ID: {r.id}. Version: {r.version}. Class: {r.rule_class.value}.",
            r.description,
            f"Overrideable: {'yes' if r.overrideable else 'no'}.",
        ])

    for rel in snapshot.all("relationships"):
        add(rel, "relationship", f"{rel.left_table.rsplit('.', 1)[1]} to "
                                 f"{rel.right_table.rsplit('.', 1)[1]}", [
            f"Relationship ID: {rel.id}. Version: {rel.version}. Role: {rel.role}.",
            rel.description,
            f"Cardinality: {rel.cardinality.value}."
            + (" Temporal as-of join." if rel.is_temporal else ""),
        ])

    for g in snapshot.all("glossary"):
        add(g, "glossary", g.term, [
            g.definition,
            f"Resolves to: {', '.join(g.maps_to)}." if g.maps_to else "",
        ], aliases=g.synonyms)

    return cards


def embed_cards(cards: list[Card], client) -> list[Card]:
    """One embedding per atomic card. Cards are short enough not to need chunking; only
    longer prose would be split."""
    texts = [f"{c.title}\n{c.content}" for c in cards]
    vectors = client.embed_documents(texts)
    for card, vector in zip(cards, vectors, strict=True):
        card.embedding = vector
    return cards
