"""Metric closure, rule engine and join planning."""

from __future__ import annotations

import pytest

from app.contracts.errors import CatalogError, GovernedError
from app.semantics.rules import ClosureInput

pytestmark = pytest.mark.unit

ORDERS = "warehouse.commerce.orders"
HISTORY = "warehouse.commerce.customer_history"
ITEMS = "warehouse.commerce.order_items"


def test_metric_closure_resolves_all_dependencies(services, scope):
    c = services.metrics.resolve("finance.revenue@7", scope)
    leaves = {x.rsplit(".", 1)[1] for x in c.all_columns}
    assert leaves == {
        "order_amount_usd", "order_status", "is_test", "refund_status", "ordered_at"
    }
    assert c.required_rule_refs == ("global.exclude_internal_customers@2",)


def test_pinned_version_mismatch_blocks(services, scope):
    """A plan pinned to an old version must not silently bind to the republished one."""
    with pytest.raises(CatalogError, match="STALE_SNAPSHOT"):
        services.metrics.resolve("finance.revenue@6", scope)


def test_unsupported_dimension_blocks(services, scope):
    metric = services.catalog.get_metric("finance.revenue@7", scope)
    with pytest.raises(CatalogError):
        services.metrics.check_dimension_allowed(metric, "commerce.not_a_dimension")


def test_enum_resolves_through_the_value_set(services):
    """The model never invents the literal 'completed'."""
    assert services.catalog.resolve_enum("values.order_status@2", "completed") == "completed"
    with pytest.raises(CatalogError):
        services.catalog.resolve_enum("values.order_status@2", "COMPLETE")


def test_rule_closure_pulls_in_an_unrequested_table(services, scope):
    """The mandatory population rule requires the customer relation even though the seed
    only mentions orders."""
    closure = services.metrics.resolve("finance.revenue@7", scope)
    result = services.rules.close(
        ClosureInput(
            tables=closure.tables,
            columns=closure.all_columns,
            metric_refs=frozenset({"finance.revenue@7"}),
            entities=frozenset({"commerce.order"}),
        ),
        scope,
    )
    assert ORDERS in result.tables
    assert HISTORY in result.tables, "mandatory rule must introduce customer_history"
    assert "commerce.orders_customers_asof@2" in result.relationship_refs
    assert "global.exclude_internal_customers@2" in result.applied_rule_refs
    assert "security.tenant_scope@5" in result.applied_rule_refs


def test_closure_reaches_a_fixed_point(services, scope):
    closure = services.metrics.resolve("finance.revenue@7", scope)
    result = services.rules.close(
        ClosureInput(
            tables=closure.tables,
            columns=closure.all_columns,
            metric_refs=frozenset({"finance.revenue@7"}),
            entities=frozenset({"commerce.order"}),
        ),
        scope,
    )
    assert result.rounds < 8, "closure must converge well inside the cap"


def test_default_rule_fills_only_an_unset_slot(services, scope):
    closure = services.metrics.resolve("finance.revenue@7", scope)
    seed = ClosureInput(
        tables=closure.tables,
        columns=closure.all_columns,
        metric_refs=frozenset({"finance.revenue@7"}),
        entities=frozenset({"commerce.order"}),
    )
    assert services.rules.close(seed, scope).slots["time.timezone"] == "UTC"

    stated = ClosureInput(
        tables=closure.tables,
        columns=closure.all_columns,
        metric_refs=frozenset({"finance.revenue@7"}),
        entities=frozenset({"commerce.order"}),
        slots={"time.timezone": "America/New_York"},
    )
    result = services.rules.close(stated, scope)
    assert result.slots["time.timezone"] == "America/New_York", (
        "a default must never overwrite a value the user stated"
    )


def test_security_rule_cannot_be_declared_overrideable():
    from app.contracts.semantics import Activation, RuleClass, RuleEffect, RuleRecord

    with pytest.raises(ValueError, match="cannot be overrideable"):
        RuleRecord(
            id="security.bad", version=1, name="bad", snapshot_id="local-001",
            source_version="1", source_hash="x", domain="global", access_scope="all",
            rule_class=RuleClass.SECURITY_OBLIGATION,
            activation=Activation(op="always"),
            effect=RuleEffect(op="require_tenant_scope"),
            overrideable=True,
        )


def test_join_planner_selects_the_temporal_edge(services, scope):
    plan = services.joins.plan(
        ORDERS, frozenset({HISTORY}), scope,
        required_relationship_refs=frozenset({"commerce.orders_customers_asof@2"}),
    )
    assert plan.hops == 1
    assert plan.is_additive_safe
    assert plan.relationship_refs == ("commerce.orders_customers_asof@2",)
    assert plan.joins[0].relationship.is_temporal


def test_join_planner_blocks_fan_out_for_additive_measures(services, scope):
    """order_items is one-to-many; joining it would multiply order rows."""
    with pytest.raises(GovernedError, match="NO_APPROVED_PATH"):
        services.joins.plan(ORDERS, frozenset({ITEMS}), scope)


def test_join_planner_flags_multiplication_when_permitted(services, scope):
    plan = services.joins.plan(ORDERS, frozenset({ITEMS}), scope, forbid_multiplying=False)
    assert not plan.is_additive_safe


def test_no_path_to_an_unrelated_table(services, scope):
    with pytest.raises(GovernedError, match="NO_APPROVED_PATH"):
        services.joins.plan(ORDERS, frozenset({"warehouse.finance.invoices"}), scope)
