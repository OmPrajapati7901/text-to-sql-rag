"""Authoritative catalog records.

These are the closed-world executable vocabulary. Descriptions aid retrieval but never create
executable facts: an extracted foreign key is evidence, not approval.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.ids import (
    ObjectId,
    ObjectType,
    PublicationStatus,
    QualifiedName,
    Ref,
    SnapshotId,
    table_id_of,
)


class KeyEvidence(StrEnum):
    """How a key constraint is known. Declared is not enforced."""

    ENFORCED = "enforced"
    DECLARED = "declared"
    PROFILED = "profiled"
    UNKNOWN = "unknown"


class TimeRole(StrEnum):
    EVENT = "event"
    PROCESSING = "processing"
    INGESTION = "ingestion"
    VALIDITY_START = "validity_start"
    VALIDITY_END = "validity_end"


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    MANY_TO_ONE = "many_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_MANY = "many_to_many"


class RecordBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: ObjectId
    version: int = Field(ge=1)
    object_type: ObjectType
    snapshot_id: SnapshotId
    source_version: str
    source_hash: str
    publication_status: PublicationStatus = PublicationStatus.APPROVED
    domain: str
    access_scope: str = Field(description="Discovery scope label checked by the PDP")
    owner: str | None = None

    @property
    def ref(self) -> Ref:
        return Ref(id=self.id, version=self.version)


class ColumnRecord(RecordBase):
    """A column. `table_id` is explicit: the sample sidecars in DESIGN/ omit it, and
    table-scoped column search cannot work without it."""

    object_type: ObjectType = ObjectType.COLUMN
    table_id: ObjectId
    name: str
    data_type: str
    nullable: bool
    description: str = ""
    synonyms: tuple[str, ...] = ()
    unit: str | None = None
    currency: str | None = None
    time_role: TimeRole | None = None
    storage_timezone: str | None = None
    value_set_ref: str | None = None
    classification: str = "internal"
    is_tenant_key: bool = False

    @model_validator(mode="after")
    def _parent_matches_id(self) -> ColumnRecord:
        if table_id_of(self.id) != self.table_id:
            raise ValueError(
                f"Column {self.id!r} declares parent {self.table_id!r} but its ID implies "
                f"{table_id_of(self.id)!r}"
            )
        return self


class TableRecord(RecordBase):
    object_type: ObjectType = ObjectType.TABLE
    physical: QualifiedName
    name: str
    description: str = ""
    business_description: str = ""
    synonyms: tuple[str, ...] = ()
    grain: tuple[ObjectId, ...] = Field(description="Column IDs forming the row grain")
    primary_key: tuple[ObjectId, ...] = ()
    key_evidence: KeyEvidence = KeyEvidence.UNKNOWN
    tenant_key: ObjectId | None = None
    is_certified: bool = Field(
        default=False, description="Certified surfaces may be executed automatically"
    )
    tags: tuple[str, ...] = ()
    metric_refs: tuple[str, ...] = ()
    rule_refs: tuple[str, ...] = ()
    approved_relationships: tuple[str, ...] = ()
    prohibited_usages: tuple[str, ...] = ()
    freshness_watermark: datetime | None = None


class JoinPredicate(BaseModel):
    """One conjunct of an approved join predicate. The compiler renders these; a model never
    writes a join expression."""

    model_config = ConfigDict(frozen=True)

    op: str = Field(description="eq | gte | lt | lt_or_null")
    left_column: ObjectId
    right_column: ObjectId


class RelationshipRecord(RecordBase):
    """An approved, role-labelled edge. Multiple edges may connect the same pair of tables with
    different business meanings (order date vs shipment date)."""

    object_type: ObjectType = ObjectType.RELATIONSHIP
    left_table: ObjectId
    right_table: ObjectId
    role: str = Field(description="Business role, e.g. purchasing_customer_at_order")
    predicates: tuple[JoinPredicate, ...]
    cardinality: Cardinality
    join_type: str = "inner"
    is_temporal: bool = False
    temporal_fact_column: ObjectId | None = None
    requires_uniqueness_contract: bool = False
    uniqueness_contract_ref: str | None = None
    prohibited_usages: tuple[str, ...] = ()
    description: str = ""

    @model_validator(mode="after")
    def _temporal_needs_fact_column(self) -> RelationshipRecord:
        if self.is_temporal and not self.temporal_fact_column:
            raise ValueError(f"Temporal relationship {self.id!r} must name its fact event column")
        return self


class GlossaryRecord(RecordBase):
    object_type: ObjectType = ObjectType.GLOSSARY
    term: str
    definition: str
    synonyms: tuple[str, ...] = ()
    maps_to: tuple[str, ...] = Field(default=(), description="Refs this term resolves to")


class DomainRecord(RecordBase):
    object_type: ObjectType = ObjectType.DOMAIN
    name: str
    description: str = ""
    default_timezone: str = "UTC"


class ValueSetRecord(RecordBase):
    """Authoritative code list. The compiler resolves enum refs through this; the model never
    invents a literal like 'completed'."""

    object_type: ObjectType = ObjectType.VALUE_SET
    members: dict[str, str] = Field(description="logical key -> exact stored value")
    is_complete: bool = True


class SnapshotManifest(BaseModel):
    """Immutable publication record for a bundle."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: SnapshotId
    published_at: datetime
    dialect: str
    object_count: int
    manifest_hash: str
    schema_fingerprint: str = Field(description="Hash of certified physical schema, for drift")
    state: PublicationStatus = PublicationStatus.APPROVED
    notes: str = ""
