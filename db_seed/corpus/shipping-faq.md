# Shipping FAQ

Common questions about how Acme Co ships orders, what carriers we use, and what to expect after `shipped_at` is set on an order.

## How long does standard shipping take?

Standard shipping is 3–5 business days within the contiguous US. We dispatch from our Ohio fulfillment center; rural ZIP codes may add 1–2 days. Standard shipping is free for orders over $50 and is calculated at checkout for smaller orders.

Standard shipping is not available to PO boxes; the cart will surface a carrier change at checkout if the address is a PO box.

## How long does expedited shipping take?

Expedited shipping is 1–2 business days within the contiguous US. Orders placed before 2 PM Eastern ship the same day; orders placed after 2 PM ship the next business day. Expedited shipping is never free and is not eligible for the loyalty-tier shipping discount.

## How long does international shipping take?

International shipping varies by destination. Most European and Asia-Pacific destinations receive orders within 7–14 business days; Latin American and Middle Eastern destinations may take 10–21 business days. Customs delays are common and are outside our control.

International orders may incur import duties and taxes assessed by the destination country. These charges are the customer's responsibility and are collected by the carrier at delivery.

## Which carriers do we use?

Standard domestic shipping uses USPS or UPS Ground depending on package weight; expedited uses FedEx Express. International orders ship via DHL Express for most destinations and EMS for select countries.

The carrier choice is recorded on each order in the fulfillment system but is not exposed on the `orders` table. Customers receive the tracking number via email once `shipped_at` is set.

## When does the shipped_at timestamp get updated?

`shipped_at` is set when the carrier scans the package at our fulfillment center, not when the label is generated. There is typically a 2–6 hour gap between label generation and carrier pickup, especially on weekends and holidays.

For orders where `shipped_at` has been set but the tracking number shows no movement after 48 hours, contact customer service — the package may have been mis-scanned and requires investigation.

## What happens on weekends and holidays?

Standard and expedited shipping operate Monday through Friday only. Orders placed on Saturday or Sunday process on the following Monday. The fulfillment center observes ten US federal holidays per year; the published holiday calendar is available in the customer portal.

International carriers operate on their own calendars; weekend processing varies by destination.
