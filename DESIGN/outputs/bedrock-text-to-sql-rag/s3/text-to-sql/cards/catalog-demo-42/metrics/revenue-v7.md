# Revenue
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
