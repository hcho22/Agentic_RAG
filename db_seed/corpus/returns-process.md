# Returns Process

The mechanical steps for processing a return, distinct from the refund policy decision. A return is the physical movement of product back to Acme Co; a refund is the financial settlement. Most refunds are paired with a return, but not all.

## Cancellation vs Return

If the order has not yet shipped (`shipped_at IS NULL`), the customer requests a cancellation rather than a return. Cancellations are free, instant, and refund the full order total. The cancellation cutoff is when the order enters the fulfillment-processing state — typically 2 to 4 hours after `paid_at` on weekdays and up to 24 hours on weekends.

Once `shipped_at` is set, cancellation is no longer available; the customer must initiate a return.

## Return Authorization

All returns require a Return Merchandise Authorization (RMA) number issued by customer service. The customer cannot ship product back without an RMA — unauthorized returns are refused at the warehouse and shipped back to the customer at the customer's expense.

The RMA is generated through the customer portal or by emailing customer service. The RMA includes a pre-paid shipping label for US domestic returns; international returns are at the customer's expense unless the return is for a wrong-item-shipped or damaged-in-transit case.

## Return Shipping

US domestic returns use USPS or UPS Ground depending on package weight. The customer drops the package at any USPS or UPS pickup point; no scheduled pickup is needed. The pre-paid label tracks the package; the customer should keep the tracking number.

Return shipping is free for the following cases:

- Damaged in transit (Acme Co's fault via the carrier).
- Wrong item shipped (Acme Co's fault).
- Product defect (Acme Co's fault).

Return shipping is paid by the customer (deducted from the refund or charged to the original payment method) for:

- Customer changed their mind.
- Customer ordered the wrong size or color.
- Customer no longer needs the item.

The deduction is $7.95 for standard returns under 5 lbs; $14.95 for returns 5–20 lbs; quoted per-case for returns over 20 lbs.

## Receiving and Inspection

Returns are received at our Ohio returns center, typically 4–7 business days after the customer drops the package. The receiving team performs an inspection: condition of product, completeness (all original parts, manuals, accessories), and match against the RMA.

The inspection outcome drives the refund settlement:

- **Pass** (product in resaleable condition): full refund per policy.
- **Partial pass** (product usable but not resaleable, e.g. opened software, worn apparel): partial refund at 50–80% of original price.
- **Fail** (product damaged by the customer, incomplete, or wrong item returned): no refund; the customer is contacted to authorize return-to-customer at the customer's expense or disposal at the customer's option.

## Return Volume Tracking

The returns center tracks return volume by SKU and surfaces SKUs with a return rate above 8% to the merchandising team for quality review. The return-rate metric is computed as returns received in the trailing 90 days divided by units sold in the same window.

A SKU placed on quality review may be temporarily de-listed from the storefront pending investigation. De-listed SKUs do not affect existing orders or open returns.
