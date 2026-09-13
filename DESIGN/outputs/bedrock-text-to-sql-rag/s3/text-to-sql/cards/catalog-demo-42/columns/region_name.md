# Current customer region
Column: warehouse.commerce.customers.region_name
Type: VARCHAR, NOT NULL.
Meaning: Current region assigned to the customer.
Synonyms: customer region, sales region.
Usable dimension for finance.revenue@7 through commerce.orders_customers@2.
Historical region at purchase is unavailable from this table.
