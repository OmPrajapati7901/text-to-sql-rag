# Refund status
Column: warehouse.commerce.orders.refund_status
Type: VARCHAR, NOT NULL.
Approved demo values: none, partially_refunded, fully_refunded.
finance.revenue@7 excludes fully_refunded orders.
This version does not subtract partial refunds; it includes the original
order_amount_usd for partially refunded orders. Do not infer net revenue.
