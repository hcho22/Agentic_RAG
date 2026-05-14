# Loyalty Program

Acme Co's loyalty program rewards repeat customers with tiered benefits based on annual spend.

## Tiers

There are four tiers: Bronze, Silver, Gold, and Platinum. Tier is recalculated on the first day of each calendar month based on the customer's spend over the trailing 12 months.

Tier thresholds:

- **Bronze**: Any customer with at least one order. No spend threshold.
- **Silver**: Trailing 12-month spend of $250 or more.
- **Gold**: Trailing 12-month spend of $1,000 or more.
- **Platinum**: Trailing 12-month spend of $5,000 or more.

Spend is computed as the sum of `orders.total` minus any `refunds.amount`, restricted to orders with `status` in `paid`, `shipped`, or `refunded`. Cancelled and pending orders do not count toward tier.

## Benefits

Bronze members receive early access to seasonal sales (24 hours before public launch) and a free birthday gift redeemable in the month of the customer's birthday.

Silver members receive everything in Bronze plus free standard shipping on orders over $25 (instead of the $50 threshold) and a 5% loyalty discount automatically applied at checkout on orders over $100.

Gold members receive everything in Silver plus free expedited shipping on orders over $75, a 10% loyalty discount on all orders, and access to the goodwill refund exception described in the Refund Policy. Gold members may also request expedited warranty replacement for apparel, home goods, and books (not electronics).

Platinum members receive everything in Gold plus a dedicated customer service line, complimentary gift wrapping on every order, and an annual $100 store credit issued on the customer's program anniversary.

## How tier is communicated

The customer's current tier appears in the account header and on every order confirmation email. Tier changes (up or down) trigger a notification email the morning of the recalculation. Customers who drop a tier retain the previous tier's benefits for the remainder of the calendar month in which the drop occurs.

## Cancellation and tier preservation

Customers who close their account lose all tier benefits immediately. Cancelled subscriptions and refunded orders reduce trailing-12-month spend and may trigger a tier drop at the next recalculation.

If a customer's trailing-12-month spend drops below their current tier threshold by less than 10% (for example, a Gold member falls to $920), the customer retains the tier for one additional month as a grace period. The grace period applies once per calendar year.

## Active customer definition

For tier purposes, an "active customer" is one whose `last_order_at` is within the trailing 90 days. Inactive customers retain their tier through the next recalculation but are not counted in the active-customer metrics reported in the monthly business review.
