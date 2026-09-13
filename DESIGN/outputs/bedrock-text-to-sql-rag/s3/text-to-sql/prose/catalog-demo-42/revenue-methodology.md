# Revenue reporting methodology
This fictional methodology explains finance.revenue@7. It does not replace
the executable metric registry.

## Population and partial refunds
Revenue counts completed, non-test orders whose refund status is not fully
refunded. Internal customers are excluded. Version 7 retains the original
order amount for partially refunded orders. A request for net revenue after
all refunds needs a different approved definition and supporting data.

## Customer geography
Reports by region use the customer's current region from the current profile.
The demo has no historical customer-region table. A request for region at the
time of purchase must report missing data rather than use current region.

## Time boundaries
The metric uses ordered_at and UTC. January 2026 means timestamps greater
than or equal to 2026-01-01T00:00:00Z and less than 2026-02-01T00:00:00Z.
Do not use an inclusive midnight upper bound for the next month.
