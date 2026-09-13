# Exclude internal customers
Rule ID: global.exclude_internal_customers. Version: 2.
Scope: All analytics using the customer population in this demo.
Effect: Include only customers for which customers.is_internal = false.
For orders, reach customers through commerce.orders_customers@2.
The rule applies before aggregation. It may introduce customers even when
the user does not request a customer dimension.
This mandatory business population rule cannot be removed by the prompt.
Canonical reference: registry:global.exclude_internal_customers@2.
