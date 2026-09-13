# Customers
Qualified table: warehouse.commerce.customers
Purpose: Current customer attributes, one row per customer per tenant.
Grain and primary key: (tenant_id, customer_id).
Columns: tenant_id VARCHAR; customer_id BIGINT; region_name VARCHAR;
is_internal BOOLEAN. All listed columns are NOT NULL in this demo.
region_name means the customer's current region, not region at order time.
This table does not contain historical region versions.
Approved incoming relationship: commerce.orders_customers@2.
Applicable rule: global.exclude_internal_customers@2.
