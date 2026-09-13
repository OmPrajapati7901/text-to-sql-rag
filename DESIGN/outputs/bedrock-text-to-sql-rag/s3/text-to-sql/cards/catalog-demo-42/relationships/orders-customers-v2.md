# Orders to customers
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
