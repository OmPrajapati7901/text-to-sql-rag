# Order amount in USD
Column: warehouse.commerce.orders.order_amount_usd
Type: DECIMAL(18,2), NOT NULL. Unit: USD.
Meaning: Order amount converted to USD upstream, stored at order grain.
Synonyms: order amount, order value, USD order total.
SUM of this field alone is not the official Revenue metric.
For Revenue, resolve finance.revenue@7 and its required population rule.
