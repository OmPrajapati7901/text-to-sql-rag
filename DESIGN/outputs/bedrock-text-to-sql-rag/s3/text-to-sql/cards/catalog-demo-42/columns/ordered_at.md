# Order timestamp
Column: warehouse.commerce.orders.ordered_at
Type: TIMESTAMPTZ, NOT NULL.
Meaning: Timestamp when the order was placed.
Used as the time dimension of finance.revenue@7.
The metric uses UTC calendar boundaries and half-open time intervals.
Synonyms: order date, order month, order time.
