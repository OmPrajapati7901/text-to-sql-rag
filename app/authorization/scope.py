"""Scope establishment — the only place a TrustedScope is created.

The caller supplies an authenticated principal, not a claimed one. Tenant, purpose, discovery
scope and permissions are all derived server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.authorization.policy import PolicyEngine
from app.contracts.scope import DiscoveryRestriction, TrustedScope, _mint


def establish_scope(
    engine: PolicyEngine,
    authenticated_principal_id: str,
    snapshot_id: str,
    purpose: str | None = None,
) -> TrustedScope:
    """Derive the trusted scope. `authenticated_principal_id` must come from a verified
    session — never from a request body field or a model tool argument."""
    policy = engine.principal(authenticated_principal_id)
    restriction = DiscoveryRestriction(
        tenant_id=policy.tenant_id,
        access_scopes=policy.access_scopes,
        allowed_domains=policy.domains or None,
        denied_object_ids=engine.denied_object_ids(authenticated_principal_id),
        snapshot_id=snapshot_id,
        publication_status="approved",
    )
    return _mint(
        principal_id=policy.principal_id,
        tenant_id=policy.tenant_id,
        purpose=purpose or policy.purpose,
        policy_epoch=engine.policy_epoch,
        snapshot_id=snapshot_id,
        discovery=restriction,
        established_at=datetime.now(UTC),
    )


def reauthorize(engine: PolicyEngine, scope: TrustedScope) -> TrustedScope:
    """Re-derive scope on resume and before release. A changed policy epoch means the old
    scope is stale and every inherited binding must be rechecked."""
    return establish_scope(engine, scope.principal_id, scope.snapshot_id, scope.purpose)
