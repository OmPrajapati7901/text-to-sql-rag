"""Reviewed source records for the demo commerce catalog.

Derived from the fictional catalog in DESIGN/outputs/ (catalog-demo-42), upgraded to the
architecture's richer model: `customers` becomes an SCD2 `customer_history`, because the
temporal attribution edge in §39 is the point of the whole slice.

This module is the *source of truth*. The publisher compiles it into an immutable snapshot
and, separately, into search projections. Nothing here is a search document.
"""

from __future__ import annotations

from typing import Any

SNAPSHOT_ID = "local-001"
DIALECT = "duckdb"

W = "warehouse.commerce"
F = "warehouse.finance"
S = "warehouse.support"
M = "warehouse.marketing"

COMMERCE = {"domain": "commerce", "access_scope": "commerce-analysts"}
FINANCE = {"domain": "finance", "access_scope": "finance-analysts"}
SUPPORT = {"domain": "support", "access_scope": "support-analysts"}
MARKETING = {"domain": "marketing", "access_scope": "marketing-analysts"}
GLOBAL = {"domain": "global", "access_scope": "all"}


def _cols(table_id: str, version: int, scope: dict, specs: list[tuple]) -> list[dict]:
    out = []
    for name, dtype, nullable, extra in specs:
        rec: dict[str, Any] = {
            "id": f"{table_id}.{name}",
            "version": version,
            "table_id": table_id,
            "name": name,
            "data_type": dtype,
            "nullable": nullable,
            "source_version": str(version),
            **scope,
        }
        rec.update(extra)
        out.append(rec)
    return out


DOMAINS = [
    {"id": "domain.commerce", "version": 1, "name": "Commerce",
     "description": "Orders, customers and the official Revenue metric.",
     "default_timezone": "UTC", "source_version": "1", **COMMERCE},
    {"id": "domain.finance", "version": 1, "name": "Finance",
     "description": "Invoicing and payments. Not the source of order Revenue.",
     "default_timezone": "UTC", "source_version": "1", **FINANCE},
    {"id": "domain.support", "version": 1, "name": "Support",
     "description": "Customer support tickets.", "default_timezone": "UTC",
     "source_version": "1", **SUPPORT},
    {"id": "domain.marketing", "version": 1, "name": "Marketing",
     "description": "Campaigns and attribution.", "default_timezone": "UTC",
     "source_version": "1", **MARKETING},
]

TABLES = [
    {
        "id": f"{W}.orders", "version": 3, "name": "orders", "is_certified": True,
        "physical": {"database": "warehouse", "schema_name": "commerce", "name": "orders"},
        "description": "Order headers; one row per tenant and order.",
        "business_description": "Customer orders used for the governed Revenue metric.",
        "synonyms": ["order headers", "purchases", "sales orders"],
        "grain": [f"{W}.orders.tenant_id", f"{W}.orders.order_id"],
        "primary_key": [f"{W}.orders.tenant_id", f"{W}.orders.order_id"],
        "key_evidence": "enforced", "tenant_key": f"{W}.orders.tenant_id",
        "tags": ["tenant_scoped", "fact"],
        "metric_refs": ["finance.revenue@7"],
        "rule_refs": ["security.tenant_scope@5", "global.exclude_internal_customers@2"],
        "approved_relationships": ["commerce.orders_customers_asof@2", "commerce.order_items@2"],
        "prohibited_usages": ["sum_order_amount_after_item_expansion"],
        "source_version": "3", **COMMERCE,
    },
    {
        "id": f"{W}.customer_history", "version": 2, "name": "customer_history",
        "is_certified": True,
        "physical": {"database": "warehouse", "schema_name": "commerce",
                     "name": "customer_history"},
        "description": "Slowly changing customer dimension with half-open validity intervals.",
        "business_description": "Customer attributes as of a point in time, including region.",
        "synonyms": ["customers", "customer dimension", "customer regions"],
        "grain": [f"{W}.customer_history.tenant_id", f"{W}.customer_history.customer_id",
                  f"{W}.customer_history.valid_from"],
        "primary_key": [f"{W}.customer_history.tenant_id", f"{W}.customer_history.customer_id",
                        f"{W}.customer_history.valid_from"],
        "key_evidence": "enforced", "tenant_key": f"{W}.customer_history.tenant_id",
        "tags": ["tenant_scoped", "scd2", "dimension"],
        "rule_refs": ["security.tenant_scope@5"],
        "approved_relationships": ["commerce.orders_customers_asof@2"],
        "source_version": "2", **COMMERCE,
    },
    {
        "id": f"{W}.order_items", "version": 2, "name": "order_items", "is_certified": False,
        "physical": {"database": "warehouse", "schema_name": "commerce", "name": "order_items"},
        "description": "Order line items; one row per tenant, order and line.",
        "business_description": "Line detail. Joining this to orders multiplies order rows.",
        "synonyms": ["line items", "order lines"],
        "grain": [f"{W}.order_items.tenant_id", f"{W}.order_items.order_id",
                  f"{W}.order_items.line_number"],
        "key_evidence": "enforced", "tenant_key": f"{W}.order_items.tenant_id",
        "tags": ["tenant_scoped", "fact"],
        "rule_refs": ["security.tenant_scope@5", "commerce.order_item_revenue_guard@2"],
        "prohibited_usages": ["sum_parent_order_amount_after_expansion"],
        "source_version": "2", **COMMERCE,
    },
]

# Distractor tables: catalogued and discoverable, but not certified for execution.
_DISTRACTORS = [
    (f"{W}.products", "products", "commerce", COMMERCE, "Product master data.",
     ["catalog", "items", "skus"]),
    (f"{W}.product_categories", "product_categories", "commerce", COMMERCE,
     "Bridge table mapping products to overlapping categories.", ["category bridge"]),
    (f"{W}.categories", "categories", "commerce", COMMERCE, "Category lookup.",
     ["product categories"]),
    (f"{W}.shipments", "shipments", "commerce", COMMERCE,
     "Shipment events with their own ship date.", ["deliveries", "shipping"]),
    (f"{W}.returns", "returns", "commerce", COMMERCE, "Returned merchandise records.",
     ["rma", "refunds"]),
    (f"{S}.tickets", "tickets", "support", SUPPORT,
     "Support tickets with their own opened_at event time.", ["support tickets", "cases"]),
    (f"{F}.invoices", "invoices", "finance", FINANCE,
     "Billing invoices. Invoice totals are not order Revenue.",
     ["billing", "invoice revenue", "billed amounts"]),
    (f"{F}.payments", "payments", "finance", FINANCE, "Received payments.",
     ["cash receipts", "settlements"]),
    (f"{M}.campaigns", "campaigns", "marketing", MARKETING, "Marketing campaigns.",
     ["promotions", "marketing spend"]),
]

for tid, name, schema, scope, desc, syns in _DISTRACTORS:
    TABLES.append({
        "id": tid, "version": 1, "name": name, "is_certified": False,
        "physical": {"database": "warehouse", "schema_name": schema, "name": name},
        "description": desc, "business_description": desc, "synonyms": syns,
        "grain": [f"{tid}.tenant_id", f"{tid}.id"], "key_evidence": "declared",
        "tenant_key": f"{tid}.tenant_id", "tags": ["tenant_scoped"],
        "rule_refs": ["security.tenant_scope@5"], "source_version": "1", **scope,
    })

COLUMNS: list[dict] = []
COLUMNS += _cols(f"{W}.orders", 3, COMMERCE, [
    ("tenant_id", "VARCHAR", False,
     {"classification": "tenant_identifier", "is_tenant_key": True,
      "description": "Owning tenant. Always bound from trusted context."}),
    ("order_id", "BIGINT", False, {"description": "Order identifier.",
                                   "synonyms": ["order number"]}),
    ("customer_id", "BIGINT", False,
     {"description": "Purchasing customer.", "synonyms": ["customer"]}),
    ("ordered_at", "TIMESTAMPTZ", False,
     {"time_role": "event", "storage_timezone": "UTC",
      "description": "When the order was placed. The official Revenue time field.",
      "synonyms": ["order date", "order time", "purchase date"]}),
    ("order_amount_usd", "DECIMAL(18,2)", False,
     {"unit": "USD", "currency": "USD", "classification": "confidential",
      "description": "Order amount converted to USD upstream, at order grain. "
                     "SUM of this field alone is not the official Revenue metric.",
      "synonyms": ["order amount", "order value", "usd order total"]}),
    ("order_status", "VARCHAR", False,
     {"value_set_ref": "values.order_status@2",
      "description": "Lifecycle status.", "synonyms": ["status"]}),
    ("is_test", "BOOLEAN", False,
     {"description": "True for synthetic test orders, which Revenue excludes.",
      "synonyms": ["test order flag"]}),
    ("refund_status", "VARCHAR", False,
     {"value_set_ref": "values.refund_status@1",
      "description": "Refund state. Revenue v7 excludes fully refunded orders and "
                     "retains the original amount for partial refunds.",
      "synonyms": ["refund state"]}),
])
COLUMNS += _cols(f"{W}.customer_history", 2, COMMERCE, [
    ("tenant_id", "VARCHAR", False,
     {"classification": "tenant_identifier", "is_tenant_key": True,
      "description": "Owning tenant."}),
    ("customer_id", "BIGINT", False, {"description": "Customer identifier."}),
    ("valid_from", "TIMESTAMPTZ", False,
     {"time_role": "validity_start", "storage_timezone": "UTC",
      "description": "Inclusive start of this version's validity."}),
    ("valid_to", "TIMESTAMPTZ", True,
     {"time_role": "validity_end", "storage_timezone": "UTC",
      "description": "Exclusive end of validity. NULL means still current."}),
    ("region_name", "VARCHAR", False,
     {"description": "Customer region as of this validity interval.",
      "synonyms": ["region", "customer region", "territory", "geography"]}),
    ("is_internal", "BOOLEAN", False,
     {"description": "True for internal/employee accounts, excluded from analytics.",
      "synonyms": ["internal customer flag", "employee account"]}),
    ("is_test_customer", "BOOLEAN", False,
     {"description": "True for synthetic test customers.",
      "synonyms": ["test customer flag"]}),
])
COLUMNS += _cols(f"{W}.order_items", 2, COMMERCE, [
    ("tenant_id", "VARCHAR", False,
     {"classification": "tenant_identifier", "is_tenant_key": True,
      "description": "Owning tenant."}),
    ("order_id", "BIGINT", False, {"description": "Parent order."}),
    ("line_number", "INTEGER", False, {"description": "Line ordinal within the order."}),
    ("product_id", "BIGINT", False, {"description": "Product on this line."}),
    ("line_amount_usd", "DECIMAL(18,2)", False,
     {"unit": "USD", "currency": "USD",
      "description": "Line amount in USD. Distinct from the order header amount."}),
])
for tid, name, _schema, scope, _desc, _syns in _DISTRACTORS:
    COLUMNS += _cols(tid, 1, scope, [
        ("tenant_id", "VARCHAR", False,
         {"classification": "tenant_identifier", "is_tenant_key": True,
          "description": "Owning tenant."}),
        ("id", "BIGINT", False, {"description": f"{name} identifier."}),
    ])

RELATIONSHIPS = [
    {
        "id": "commerce.orders_customers_asof", "version": 2,
        "left_table": f"{W}.orders", "right_table": f"{W}.customer_history",
        "role": "purchasing_customer_at_order",
        "description": "Each order joins the customer version valid at its order time. "
                       "Reusing this edge for a different event time is invalid.",
        "cardinality": "many_to_one", "join_type": "inner", "is_temporal": True,
        "temporal_fact_column": f"{W}.orders.ordered_at",
        "requires_uniqueness_contract": True,
        "uniqueness_contract_ref": "quality.customer_history_non_overlap@1",
        "predicates": [
            {"op": "eq", "left_column": f"{W}.orders.tenant_id",
             "right_column": f"{W}.customer_history.tenant_id"},
            {"op": "eq", "left_column": f"{W}.orders.customer_id",
             "right_column": f"{W}.customer_history.customer_id"},
            {"op": "gte", "left_column": f"{W}.orders.ordered_at",
             "right_column": f"{W}.customer_history.valid_from"},
            {"op": "lt_or_null", "left_column": f"{W}.orders.ordered_at",
             "right_column": f"{W}.customer_history.valid_to"},
        ],
        "source_version": "2", **COMMERCE,
    },
    {
        "id": "commerce.order_items", "version": 2,
        "left_table": f"{W}.orders", "right_table": f"{W}.order_items",
        "role": "order_lines",
        "description": "One order has many lines. Expands order rows; never sum the order "
                       "header amount across this edge.",
        "cardinality": "one_to_many", "join_type": "inner", "is_temporal": False,
        "prohibited_usages": ["sum_parent_order_amount_after_expansion"],
        "predicates": [
            {"op": "eq", "left_column": f"{W}.orders.tenant_id",
             "right_column": f"{W}.order_items.tenant_id"},
            {"op": "eq", "left_column": f"{W}.orders.order_id",
             "right_column": f"{W}.order_items.order_id"},
        ],
        "source_version": "2", **COMMERCE,
    },
]

VALUE_SETS = [
    {"id": "values.order_status", "version": 2,
     "members": {"completed": "completed", "pending": "pending", "cancelled": "cancelled"},
     "is_complete": True, "source_version": "2", **GLOBAL},
    {"id": "values.refund_status", "version": 1,
     "members": {"none": "none", "partially_refunded": "partially_refunded",
                 "fully_refunded": "fully_refunded"},
     "is_complete": True, "source_version": "1", **GLOBAL},
]

METRICS = [
    {
        "id": "finance.revenue", "version": 7, "name": "Revenue",
        "description": "Sum of eligible order amounts in USD at order grain.",
        "aliases": ["revenue", "recognized order revenue", "sales revenue", "total revenue"],
        "unit": "USD", "base_entity": "commerce.order",
        "allowed_dimensions": ["customer.region_at_order"],
        "empty_set_policy": "no_matching_data", "missing_groups": "omit",
        "additivity": {"disjoint_orders": "additive",
                       "overlapping_product_groups": "not_additive"},
        "base_table": f"{W}.orders",
        "base_grain": [f"{W}.orders.tenant_id", f"{W}.orders.order_id"],
        "expression": {"kind": "aggregate", "op": "sum",
                       "operand": {"kind": "column",
                                   "column_ref": f"{W}.orders.order_amount_usd"}},
        "population": {"kind": "bool", "op": "and", "args": [
            {"kind": "compare", "op": "eq",
             "left": {"kind": "column", "column_ref": f"{W}.orders.order_status"},
             "right": {"kind": "enum", "value_set_ref": "values.order_status@2",
                       "member": "completed"}},
            {"kind": "compare", "op": "eq",
             "left": {"kind": "column", "column_ref": f"{W}.orders.is_test"},
             "right": {"kind": "literal", "type": "boolean", "value": False}},
            {"kind": "compare", "op": "ne",
             "left": {"kind": "column", "column_ref": f"{W}.orders.refund_status"},
             "right": {"kind": "enum", "value_set_ref": "values.refund_status@1",
                       "member": "fully_refunded"}},
        ]},
        "required_rule_refs": ["global.exclude_internal_customers@2"],
        "time": {"time_column": f"{W}.orders.ordered_at",
                 "grains": ["day", "month", "quarter", "year"],
                 "timezone": "UTC", "calendar_ref": "calendar.gregorian@1"},
        "dependency_refs": [f"{W}.orders@3", "commerce.orders_customers_asof@2"],
        "supersedes": "finance.revenue@6",
        "source_version": "7", **FINANCE,
    },
    {
        "id": "commerce.order_count", "version": 1, "name": "Order count",
        "description": "Count of eligible orders at order grain.",
        "aliases": ["order count", "number of orders", "orders"],
        "unit": None, "base_entity": "commerce.order",
        "allowed_dimensions": ["customer.region_at_order"],
        "base_table": f"{W}.orders",
        "base_grain": [f"{W}.orders.tenant_id", f"{W}.orders.order_id"],
        "expression": {"kind": "aggregate", "op": "count", "operand": None},
        "population": {"kind": "bool", "op": "and", "args": [
            {"kind": "compare", "op": "eq",
             "left": {"kind": "column", "column_ref": f"{W}.orders.order_status"},
             "right": {"kind": "enum", "value_set_ref": "values.order_status@2",
                       "member": "completed"}},
            {"kind": "compare", "op": "eq",
             "left": {"kind": "column", "column_ref": f"{W}.orders.is_test"},
             "right": {"kind": "literal", "type": "boolean", "value": False}},
        ]},
        "required_rule_refs": ["global.exclude_internal_customers@2"],
        "time": {"time_column": f"{W}.orders.ordered_at",
                 "grains": ["day", "month", "quarter", "year"],
                 "timezone": "UTC", "calendar_ref": "calendar.gregorian@1"},
        "dependency_refs": [f"{W}.orders@3"],
        "source_version": "1", **COMMERCE,
    },
]

DIMENSIONS = [
    {
        "id": "customer.region_at_order", "version": 2, "name": "Customer region at order",
        "description": "The customer's region as of the order event time.",
        "aliases": ["region", "customer region", "region at order", "territory"],
        "column": f"{W}.customer_history.region_name",
        "entity_role": "purchasing_customer", "temporal_binding": "at_fact_event",
        "required_relationship_ref": "commerce.orders_customers_asof@2",
        "source_version": "2", **COMMERCE,
    },
]

RULES = [
    {
        "id": "security.tenant_scope", "version": 5, "name": "Tenant isolation",
        "description": "Every scan of a tenant-scoped relation is bound to the trusted tenant.",
        "rule_class": "security_obligation",
        "activation": {"op": "any_scan_with_tag", "tag": "tenant_scoped"},
        "effect": {"op": "require_tenant_scope", "placement": "before_aggregation"},
        "overrideable": False, "failure": "deny", "priority_within_class": 0,
        "source_version": "5", **GLOBAL,
    },
    {
        "id": "global.exclude_internal_customers", "version": 2,
        "name": "Exclude internal customers",
        "description": "Analytics include only external, non-test customers. This rule can "
                       "introduce the customer relation even when no customer dimension was "
                       "requested.",
        "rule_class": "mandatory_business_population",
        "activation": {"op": "population_contains_entity", "entity": "commerce.order"},
        "effect": {
            "op": "require_population_filter", "placement": "before_aggregation",
            "temporal_binding": "at_fact_event",
            "required_tables": [f"{W}.customer_history"],
            "required_relationship_ref": "commerce.orders_customers_asof@2",
            "unmatched_policy": "quality_failure",
            "predicate": {"kind": "bool", "op": "and", "args": [
                {"kind": "compare", "op": "eq",
                 "left": {"kind": "column",
                          "column_ref": f"{W}.customer_history.is_internal"},
                 "right": {"kind": "literal", "type": "boolean", "value": False}},
                {"kind": "compare", "op": "eq",
                 "left": {"kind": "column",
                          "column_ref": f"{W}.customer_history.is_test_customer"},
                 "right": {"kind": "literal", "type": "boolean", "value": False}},
            ]},
        },
        "overrideable": False, "failure": "block_metric", "priority_within_class": 10,
        "source_version": "2", **GLOBAL,
    },
    {
        "id": "commerce.order_item_revenue_guard", "version": 2,
        "name": "Order item revenue guard",
        "description": "Summing the order header amount after expanding to line items "
                       "double counts. Requires a semi-join or an approved allocation.",
        "rule_class": "semantic_requirement",
        "activation": {"op": "uses_table", "object_id": f"{W}.order_items"},
        "effect": {"op": "require_semijoin_or_allocation"},
        "overrideable": False, "failure": "clarify_or_unavailable",
        "priority_within_class": 20, "source_version": "2", **COMMERCE,
    },
    {
        "id": "commerce.reporting_timezone", "version": 1, "name": "Reporting timezone",
        "description": "Commerce reporting defaults to UTC when the user states no timezone.",
        "rule_class": "default",
        "activation": {"op": "slot_unset", "slot": "time.timezone"},
        "effect": {"op": "set_slot", "slot": "time.timezone", "slot_value": "UTC"},
        "overrideable": True, "allowed_override": "explicit_supported_user_timezone",
        "failure": "warn", "priority_within_class": 50, "source_version": "1", **COMMERCE,
    },
]

GLOSSARY = [
    {"id": "glossary.revenue", "version": 1, "term": "revenue",
     "definition": "The official recognized order revenue metric for commerce.",
     "synonyms": ["sales", "top line", "turnover", "recognized revenue"],
     "maps_to": ["finance.revenue@7"], "source_version": "1", **COMMERCE},
    {"id": "glossary.region", "version": 1, "term": "region",
     "definition": "Customer region, attributed as of the order event time.",
     "synonyms": ["territory", "geography", "area"],
     "maps_to": ["customer.region_at_order@2"], "source_version": "1", **COMMERCE},
    {"id": "glossary.order_count", "version": 1, "term": "order count",
     "definition": "Count of eligible completed orders.",
     "synonyms": ["number of orders", "order volume"],
     "maps_to": ["commerce.order_count@1"], "source_version": "1", **COMMERCE},
]

# Physical schema the certified surface expects. Drift against the live database is a
# blocking condition: a pinned catalog does not freeze the real database.
CERTIFIED_PHYSICAL_SCHEMA = {
    f"{W}.orders": ["tenant_id", "order_id", "customer_id", "ordered_at",
                    "order_amount_usd", "order_status", "is_test", "refund_status"],
    f"{W}.customer_history": ["tenant_id", "customer_id", "valid_from", "valid_to",
                              "region_name", "is_internal", "is_test_customer"],
    f"{W}.order_items": ["tenant_id", "order_id", "line_number", "product_id",
                         "line_amount_usd"],
}
