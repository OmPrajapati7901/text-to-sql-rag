# Orders
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
