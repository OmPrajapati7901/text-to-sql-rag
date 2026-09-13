"""Stable identifiers and versioned references.

Object IDs are opaque, namespaced strings owned by the catalog. The LLM selects them; it never
constructs executable names from them. A reference pins an exact version so a plan cannot drift
when a definition is republished.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# domain.name / warehouse.schema.table / warehouse.schema.table.column
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

ObjectId = Annotated[str, Field(pattern=_ID_RE.pattern, min_length=3, max_length=200)]
SnapshotId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9\-]{2,63}$")]


class ObjectType(StrEnum):
    DOMAIN = "domain"
    TABLE = "table"
    COLUMN = "column"
    METRIC = "metric"
    DIMENSION = "dimension"
    RULE = "rule"
    RELATIONSHIP = "relationship"
    GLOSSARY = "glossary"
    VALUE_SET = "value_set"


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class Ref(BaseModel):
    """A pinned reference: `finance.revenue@7`."""

    model_config = ConfigDict(frozen=True)

    id: ObjectId
    version: int = Field(ge=1)

    @classmethod
    def parse(cls, text: str) -> Ref:
        if "@" not in text:
            raise ValueError(f"Reference must pin a version: {text!r}")
        obj_id, _, version = text.rpartition("@")
        if not version.isdigit():
            raise ValueError(f"Reference version must be an integer: {text!r}")
        return cls(id=obj_id, version=int(version))

    def __str__(self) -> str:
        return f"{self.id}@{self.version}"


class QualifiedName(BaseModel):
    """A physical location. Rendered only by the compiler, never by a model."""

    model_config = ConfigDict(frozen=True)

    database: str
    schema_name: str
    name: str

    @field_validator("database", "schema_name", "name")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        # The compiler quotes identifiers, but a name that needs escaping never reaches us
        # from a governed catalog. Reject rather than escape, so catalog defects surface.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"Unsafe physical identifier: {value!r}")
        return value

    def __str__(self) -> str:
        return f"{self.database}.{self.schema_name}.{self.name}"


def table_id_of(column_id: str) -> str:
    """Parent table of a column ID. Columns always carry an explicit parent, but this
    derivation catches bundle defects where the two disagree."""
    parent, _, _ = column_id.rpartition(".")
    if not parent:
        raise ValueError(f"Column ID has no parent table: {column_id!r}")
    return parent
