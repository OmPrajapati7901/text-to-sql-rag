from pathlib import Path
import hashlib
import json

ROOT = Path('outputs/bedrock-text-to-sql-rag')
SNAPSHOT = 'catalog-demo-42'
cards = [
('domains/commerce.md', 'domain', 'domain.commerce', '1', '''# Commerce analytics
Purpose: Analyze orders, customers and the approved Revenue metric.
Business terms: revenue, sales, customer region, completed order.
Primary tables: warehouse.commerce.orders and warehouse.commerce.customers.
Official metric: finance.revenue, version 7.
Customer attribution in this demo uses the current customer region.
All objects in this sample are fictional and approved only within this demo.
'''),
('tables/orders.md', 'table', 'warehouse.commerce.orders', '3', '''# Orders
Qualified table: warehouse.commerce.orders
Purpose: One record per customer order. This is not an order-line table.
Grain: One row per (tenant_id, order_id).
Primary key: (tenant_id, order_id).
Columns: tenant_id VARCHAR; order_id BIGINT; customer_id BIGINT;
ordered_at TIMESTAMPTZ; order_amount_usd DECIMAL(18,2);
order_status VARCHAR; is_test BOOLEAN; refund_status VARCHAR.
All listed columns are NOT NULL in this demo.
order_amount_usd is already converted to USD upstream.
Approved relationship: commerce.orders_customers@2.
Associated official metric: finance.revenue@7.
Applicable customer-population rule: global.exclude_internal_customers@2.
Do not substitute order lines or sum an amount at a different grain.
'''),
('tables/customers.md', 'table', 'warehouse.commerce.customers', '2', '''# Customers
Qualified table: warehouse.commerce.customers
Purpose: Current customer attributes, one row per customer per tenant.
Grain and primary key: (tenant_id, customer_id).
Columns: tenant_id VARCHAR; customer_id BIGINT; region_name VARCHAR;
is_internal BOOLEAN. All listed columns are NOT NULL in this demo.
region_name means the customer's current region, not region at order time.
This table does not contain historical region versions.
Approved incoming relationship: commerce.orders_customers@2.
Applicable rule: global.exclude_internal_customers@2.
'''),
('columns/order_amount_usd.md', 'column', 'warehouse.commerce.orders.order_amount_usd', '3', '''# Order amount in USD
Column: warehouse.commerce.orders.order_amount_usd
Type: DECIMAL(18,2), NOT NULL. Unit: USD.
Meaning: Order amount converted to USD upstream, stored at order grain.
Synonyms: order amount, order value, USD order total.
SUM of this field alone is not the official Revenue metric.
For Revenue, resolve finance.revenue@7 and its required population rule.
'''),
('columns/ordered_at.md', 'column', 'warehouse.commerce.orders.ordered_at', '3', '''# Order timestamp
Column: warehouse.commerce.orders.ordered_at
Type: TIMESTAMPTZ, NOT NULL.
Meaning: Timestamp when the order was placed.
Used as the time dimension of finance.revenue@7.
The metric uses UTC calendar boundaries and half-open time intervals.
Synonyms: order date, order month, order time.
'''),
('columns/order_status.md', 'column', 'warehouse.commerce.orders.order_status', '3', '''# Order status
Column: warehouse.commerce.orders.order_status
Type: VARCHAR, NOT NULL.
Approved demo values: completed, pending, cancelled.
finance.revenue@7 includes only completed orders.
The approved values come from this demo's catalog, not inferred samples.
'''),
('columns/is_test.md', 'column', 'warehouse.commerce.orders.is_test', '3', '''# Test order flag
Column: warehouse.commerce.orders.is_test
Type: BOOLEAN, NOT NULL.
True identifies a test order. finance.revenue@7 requires false.
Synonyms: test transaction, synthetic order.
'''),
('columns/refund_status.md', 'column', 'warehouse.commerce.orders.refund_status', '3', '''# Refund status
Column: warehouse.commerce.orders.refund_status
Type: VARCHAR, NOT NULL.
Approved demo values: none, partially_refunded, fully_refunded.
finance.revenue@7 excludes fully_refunded orders.
This version does not subtract partial refunds; it includes the original
order_amount_usd for partially refunded orders. Do not infer net revenue.
'''),
('columns/region_name.md', 'column', 'warehouse.commerce.customers.region_name', '2', '''# Current customer region
Column: warehouse.commerce.customers.region_name
Type: VARCHAR, NOT NULL.
Meaning: Current region assigned to the customer.
Synonyms: customer region, sales region.
Usable dimension for finance.revenue@7 through commerce.orders_customers@2.
Historical region at purchase is unavailable from this table.
'''),
('columns/is_internal.md', 'column', 'warehouse.commerce.customers.is_internal', '2', '''# Internal customer flag
Column: warehouse.commerce.customers.is_internal
Type: BOOLEAN, NOT NULL.
True identifies an internal customer.
global.exclude_internal_customers@2 requires false for customer-population
analytics in the demo. This is a business rule, not a tenant access policy.
'''),
('metrics/revenue-v7.md', 'metric', 'finance.revenue', '7', '''# Revenue
Metric ID: finance.revenue
Version: 7. Status: approved in the demo registry.
Domain: commerce. Synonyms: revenue, recognized order revenue.
Meaning: Sum of eligible order amounts in USD at order grain.
Base measure: SUM(warehouse.commerce.orders.order_amount_usd).
Required order predicates: order_status = 'completed'; is_test = false;
refund_status <> 'fully_refunded'. These fields are NOT NULL in the demo.
Required population rule: global.exclude_internal_customers@2.
That rule requires customers.is_internal = false through the approved
commerce.orders_customers@2 relationship.
Time field: orders.ordered_at. Timezone: UTC. Intervals: start inclusive,
end exclusive. Supported time grains: day, month, year.
Supported dimension: customers.region_name, using current customer region.
Partial refunds: original order amount remains included in version 7.
Do not reinterpret this metric as net revenue after partial refunds.
Canonical reference: registry:finance.revenue@7.
Fetch the executable definition and dependencies from the registry.
'''),
('rules/exclude-internal-customers-v2.md', 'rule', 'global.exclude_internal_customers', '2', '''# Exclude internal customers
Rule ID: global.exclude_internal_customers. Version: 2.
Scope: All analytics using the customer population in this demo.
Effect: Include only customers for which customers.is_internal = false.
For orders, reach customers through commerce.orders_customers@2.
The rule applies before aggregation. It may introduce customers even when
the user does not request a customer dimension.
This mandatory business population rule cannot be removed by the prompt.
Canonical reference: registry:global.exclude_internal_customers@2.
'''),
('relationships/orders-customers-v2.md', 'relationship', 'commerce.orders_customers', '2', '''# Orders to customers
Relationship ID: commerce.orders_customers. Version: 2.
From: warehouse.commerce.orders as o.
To: warehouse.commerce.customers as c.
Approved predicates: o.tenant_id = c.tenant_id AND o.customer_id = c.customer_id.
Cardinality: Many orders to one customer; target key (tenant_id, customer_id).
Join type for the Revenue plan: INNER.
Referential integrity: Every demo order has exactly one matching customer.
Business meaning: Attribute orders to the customer's current profile.
Temporal meaning: Current region; not historical region at order time.
Tenant predicate must be present even when customer IDs appear unique.
Canonical reference: registry:commerce.orders_customers@2.
'''),
('glossary/revenue.md', 'glossary', 'glossary.revenue', '1', '''# Revenue terminology
For the commerce domain in this demo, Revenue resolves to finance.revenue@7.
The official metric excludes test orders, fully refunded orders and internal
customers. It is not simply the sum of all order amounts.
For a definition answer, cite the approved metric record.
For a data answer, execute the metric's verified plan against the database.
'''),
]

def write_source(relative, object_type, object_id, version, body, doc_class):
    path = ROOT / 's3' / 'text-to-sql' / doc_class / SNAPSHOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    meta = {'metadataAttributes': {
        'object_id': object_id, 'object_type': object_type,
        'domain': 'commerce', 'tenant_id': 'tenant-demo',
        'access_scope': 'finance-analysts-demo', 'snapshot_id': SNAPSHOT,
        'source_version': version, 'publication_status': 'approved',
        'canonical_ref': f'registry:{object_id}@{version}',
        'source_hash': hashlib.sha256(body.encode()).hexdigest(),
        'language': 'en', 'is_synthetic': True,
    }}
    Path(str(path) + '.metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
    return {'key': str(path.relative_to(ROOT / 's3')), 'object_id': object_id,
            'source_version': version, 'source_hash': meta['metadataAttributes']['source_hash']}

manifest = [write_source(*card, 'cards') for card in cards]
manifest.append(write_source('revenue-methodology.md', 'documentation',
    'documentation.revenue_methodology', '1', '''# Revenue reporting methodology
This fictional methodology explains finance.revenue@7. It does not replace
the executable metric registry.

## Population and partial refunds
Revenue counts completed, non-test orders whose refund status is not fully
refunded. Internal customers are excluded. Version 7 retains the original
order amount for partially refunded orders. A request for net revenue after
all refunds needs a different approved definition and supporting data.

## Customer geography
Reports by region use the customer's current region from the current profile.
The demo has no historical customer-region table. A request for region at the
time of purchase must report missing data rather than use current region.

## Time boundaries
The metric uses ordered_at and UTC. January 2026 means timestamps greater
than or equal to 2026-01-01T00:00:00Z and less than 2026-02-01T00:00:00Z.
Do not use an inclusive midnight upper bound for the next month.
''', 'prose'))
(ROOT / 'manifest.json').write_text(json.dumps({'synthetic': True, 'snapshot_id': SNAPSHOT,
    'source_document_count': len(manifest), 'documents': manifest}, indent=2) + '\n')
config_dir = ROOT / 'examples' / 'chunking'
config_dir.mkdir(parents=True, exist_ok=True)
configs = {
    'cards-none': {'chunkingStrategy': 'NONE'},
    'prose-fixed': {'chunkingStrategy': 'FIXED_SIZE', 'fixedSizeChunkingConfiguration':
        {'maxTokens': 512, 'overlapPercentage': 12}},
    'prose-semantic-experiment': {'chunkingStrategy': 'SEMANTIC', 'semanticChunkingConfiguration':
        {'maxTokens': 512, 'bufferSize': 1, 'breakpointPercentileThreshold': 95}},
    'prose-hierarchical-experiment': {'chunkingStrategy': 'HIERARCHICAL', 'hierarchicalChunkingConfiguration':
        {'levelConfigurations': [{'maxTokens': 1200}, {'maxTokens': 300}], 'overlapTokens': 50}},
}
for name, cfg in configs.items():
    (config_dir / (name + '.json')).write_text(json.dumps({'chunkingConfiguration': cfg}, indent=2) + '\n')
print(f'Created {len(manifest)} fictional documents, paired sidecars and four chunking configurations.')
