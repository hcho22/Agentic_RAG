# Refund Policy

This document defines how Acme Co handles refunds for orders placed through our online store and customer support channels.

## Eligibility Window

Customers may request a refund within 30 days of the order's `shipped_at` date. Orders that have not shipped follow the cancellation policy instead — see the Returns Process document for the cancellation cutoff and partial-shipment edge cases.

Refund eligibility is independent of the order's `paid_at` date. A refund window that has already closed cannot be reopened by customer service except under the goodwill exception described below.

## Full vs Partial Refunds

A full refund returns the entire order `total` (including tax, shipping, and any applied discounts already debited). Full refunds are the default for damaged-in-transit, wrong-item-shipped, and product-defect cases.

A partial refund returns a subset of the order `total`. Partial refunds are the default for customer-dissatisfied and delivery-delay cases; the refunded amount is typically 10% to 80% of the order total and is set at the agent's discretion within the published guardrails.

The `refunds.amount` field records the refunded value in the same currency as `orders.total`. Net revenue calculations subtract `refunds.amount` from gross revenue.

## Goodwill Exception

Customer service may issue a goodwill refund outside the 30-day window when:

- The order shipped through a third-party carrier that lost the package after delivery scan.
- The customer can demonstrate that the product became defective within the first 90 days but the defect was not discoverable on receipt.
- A loyalty-tier customer (Gold or Platinum) requests a refund within 60 days of `shipped_at`.

Goodwill refunds are capped at 50% of the order total unless escalated to a supervisor. Document the goodwill reason in the `refunds.reason` field for auditability.

## Refund Method

Refunds are processed to the original payment method whenever possible. Card refunds typically settle in 5–7 business days; bank transfers in 3–5 business days. Store credit is offered as an alternative and is available immediately.

Refunds cannot be split across multiple payment methods. If the customer paid with a combination of store credit and card, the refund returns to each method proportionally to the original split.

## What Refunds Do Not Cover

Refunds do not include the original shipping cost on orders shipped via expedited or international carriers, except for the damaged-in-transit and wrong-item-shipped cases. Restocking fees do not apply at Acme Co; we do not deduct from the refund amount for handling.

Gift cards are not refundable. Subscription-style orders follow the Loyalty Program cancellation rules, not this policy.
