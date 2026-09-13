"""Trusted authorization scope.

The scope is derived server-side from authenticated context. It is never parsed from a request
body, never supplied by an LLM tool argument, and never checkpointed into graph state (state
carries an opaque `policy_ref` instead).

Construction is deliberately awkward from the outside: `TrustedScope` requires a construction
token that only `app.authorization.scope` can mint. This makes "the model fabricated a scope"
a type error rather than a code review finding.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.ids import SnapshotId

# Minted once at import. Only modules that can read this attribute can build a TrustedScope.
_CONSTRUCTION_TOKEN = secrets.token_urlsafe(32)


class Action(StrEnum):
    """Distinct actions. A table grant does not imply every-column access, and reading a
    metric's value does not imply reading its underlying measure column."""

    DISCOVER = "discover"
    READ = "read"
    USE_IN_PREDICATE = "use_in_predicate"
    EXECUTE_METRIC = "execute_metric"
    DISCLOSE = "disclose"


class DiscoveryRestriction(BaseModel):
    """A normalized, provider-neutral discovery restriction.

    Providers translate this into their own query language. OpenSearch DSL and Bedrock filter
    objects never travel through the application or the graph.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    access_scopes: frozenset[str] = Field(default_factory=frozenset)
    allowed_domains: frozenset[str] | None = Field(
        default=None, description="None means no domain restriction; empty set means nothing"
    )
    denied_object_ids: frozenset[str] = Field(default_factory=frozenset)
    snapshot_id: str
    publication_status: str = "approved"

    def is_empty(self) -> bool:
        """True when the restriction admits nothing. Providers must fail closed, not open."""
        return self.allowed_domains is not None and not self.allowed_domains


class TrustedScope(BaseModel):
    """Established by authorization from authenticated context. Immutable for a request."""

    model_config = ConfigDict(frozen=True)

    principal_id: str
    tenant_id: str
    purpose: str
    policy_epoch: int = Field(ge=1)
    snapshot_id: SnapshotId
    discovery: DiscoveryRestriction
    established_at: datetime
    token: str = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def _verify_construction_token(self) -> Self:
        if not secrets.compare_digest(self.token, _CONSTRUCTION_TOKEN):
            raise ValueError(
                "TrustedScope must be established by app.authorization.scope, "
                "not constructed from request or model input"
            )
        if self.discovery.tenant_id != self.tenant_id:
            raise ValueError("Discovery restriction tenant does not match scope tenant")
        if self.discovery.snapshot_id != self.snapshot_id:
            raise ValueError("Discovery restriction snapshot does not match scope snapshot")
        return self

    @property
    def policy_ref(self) -> str:
        """The opaque handle safe to place in checkpointed state."""
        return f"policy:{self.principal_id}:{self.tenant_id}:{self.policy_epoch}"

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"<TrustedScope {self.principal_id}@{self.tenant_id} epoch={self.policy_epoch}>"


def _mint(**fields: Any) -> TrustedScope:
    """Internal constructor. Imported by app.authorization.scope only."""
    return TrustedScope(token=_CONSTRUCTION_TOKEN, **fields)
