"""Provider-neutral retrieval contract.

Search *text* is the portable input, not a precomputed query vector: each adapter owns its own
embedding behavior. Raw similarity and BM25 scores are provider diagnostics, never confidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.errors import Finding
from app.contracts.ids import ObjectId, ObjectType, SnapshotId


class CompletionStatus(StrEnum):
    """Service-response completion. Distinct from semantic evidence completeness."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class SearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    search_text: str = Field(min_length=1, max_length=4000)
    object_types: frozenset[ObjectType] = Field(default_factory=frozenset)
    table_ids: frozenset[ObjectId] = Field(
        default_factory=frozenset, description="Scope column search to these parent tables"
    )
    domains: frozenset[str] = Field(default_factory=frozenset)
    candidate_budget: int = Field(default=40, ge=1, le=500)
    snapshot_id: SnapshotId
    deadline: datetime | None = None


class Candidate(BaseModel):
    """One ranked discovery result. `excerpt` is authorized prose and is always untrusted data."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    object_id: ObjectId
    object_type: ObjectType
    table_id: ObjectId | None = None
    source_version: str
    snapshot_id: SnapshotId
    title: str
    excerpt: str
    source_ref: str
    rank: int = Field(ge=1)
    provider_score: float | None = Field(
        default=None,
        description="Provider-specific diagnostic. Not a probability; never gate on it.",
    )


class ProviderDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    status: CompletionStatus
    took_ms: int | None = None
    keyword_hits: int = 0
    vector_hits: int = 0
    shard_failures: int = 0
    findings: tuple[Finding, ...] = ()


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...]
    diagnostics: ProviderDiagnostics
    next_cursor: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.diagnostics.status is CompletionStatus.COMPLETE


class ProviderCapabilities(BaseModel):
    """What an adapter can faithfully do. Used to fail closed rather than silently degrade."""

    model_config = ConfigDict(frozen=True)

    name: str
    supports_keyword: bool = True
    supports_vector: bool = True
    supports_domain_filter: bool = True
    supports_table_scoped_columns: bool = True
    supports_denied_object_filter: bool = True
    embedding_dimensions: int | None = None
