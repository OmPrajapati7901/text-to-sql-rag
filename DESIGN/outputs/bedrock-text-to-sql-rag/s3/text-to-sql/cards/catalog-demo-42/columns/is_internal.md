# Internal customer flag
Column: warehouse.commerce.customers.is_internal
Type: BOOLEAN, NOT NULL.
True identifies an internal customer.
global.exclude_internal_customers@2 requires false for customer-population
analytics in the demo. This is a business rule, not a tenant access policy.
