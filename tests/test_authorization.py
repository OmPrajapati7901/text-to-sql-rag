"""Authorization negative tests.

Every one of these must deny. A passing test here is a test that failed to get access.
"""

from __future__ import annotations

import pytest

from app.contracts.errors import AuthorizationError
from app.contracts.scope import Action, DiscoveryRestriction, TrustedScope

pytestmark = pytest.mark.unit

AMOUNT = "warehouse.commerce.orders.order_amount_usd"


def test_forged_scope_is_rejected(services):
    """A TrustedScope cannot be constructed without the authorization module's token."""
    from datetime import UTC, datetime

    restriction = DiscoveryRestriction(tenant_id="tenant-demo", snapshot_id="local-001")
    with pytest.raises(ValueError, match="must be established by"):
        TrustedScope(
            principal_id="attacker",
            tenant_id="tenant-demo",
            purpose="analytics",
            policy_epoch=1,
            snapshot_id="local-001",
            discovery=restriction,
            established_at=datetime.now(UTC),
            token="guessed-token",
        )


def test_scope_token_never_serializes(scope):
    """The construction token must not leak into a checkpoint."""
    assert "token" not in scope.model_dump()
    assert "token" not in scope.model_dump_json()


def test_denied_column_cannot_be_read(services):
    assert not services.policy.check("analyst_restricted", Action.READ, AMOUNT).allowed


def test_denied_column_cannot_be_used_in_a_predicate(services):
    """Failure mode #25: masking a projection does not stop inference through filtering
    or sorting. A READ deny must cascade to USE_IN_PREDICATE."""
    decision = services.policy.check("analyst_restricted", Action.USE_IN_PREDICATE, AMOUNT)
    assert not decision.allowed
    assert "cascades" in decision.reason


def test_denied_column_cannot_be_disclosed(services):
    assert not services.policy.check("analyst_restricted", Action.DISCLOSE, AMOUNT).allowed


def test_discover_deny_cascades_to_everything(services):
    """analyst_restricted is denied discovery of the finance domain."""
    for action in (Action.DISCOVER, Action.READ, Action.USE_IN_PREDICATE):
        assert not services.policy.check(
            "analyst_restricted", action, "warehouse.finance.invoices"
        ).allowed


def test_table_grant_does_not_imply_denied_column(services, restricted_scope):
    """A table-level grant must not resurrect a column-level deny."""
    columns = services.catalog.get_columns(["warehouse.commerce.orders"], restricted_scope)
    names = {c.name for c in columns}
    assert "order_status" in names
    assert "order_amount_usd" not in names


def test_restricted_principal_cannot_execute_revenue(services, restricted_scope):
    with pytest.raises(AuthorizationError):
        services.metrics.resolve("finance.revenue@7", restricted_scope)


def test_mandatory_columns_fail_closed_as_a_set(services, restricted_scope):
    """Dropping an inaccessible mandatory column would silently change meaning, so the
    whole set must fail rather than degrade."""
    with pytest.raises(AuthorizationError):
        services.catalog.require_columns_usable(
            frozenset({AMOUNT, "warehouse.commerce.orders.order_status"}),
            restricted_scope,
            Action.READ,
        )


def test_unknown_principal_is_rejected(services):
    from app.authorization.scope import establish_scope

    with pytest.raises(AuthorizationError):
        establish_scope(services.policy, "does_not_exist", "local-001")


def test_tenant_isolation_is_derived_not_claimed(services, other_tenant_scope):
    """Tenant comes from the authenticated principal, never from a request field."""
    assert other_tenant_scope.tenant_id == "tenant-other"
    assert other_tenant_scope.discovery.tenant_id == "tenant-other"
