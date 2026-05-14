# Product Catalog Overview

Acme Co sells across five categories: electronics, apparel, home goods, books, and food. This document summarises the catalog structure, pricing conventions, and category-specific notes that affect order processing and reporting.

## Catalog Structure

Each product carries an SKU of the form `CAT-NNNN` where `CAT` is a three-letter category prefix and `NNNN` is a zero-padded sequence number. The category prefix is `ELE` for electronics, `APP` for apparel, `HOM` for home goods, `BOO` for books, and `FOO` for food. SKUs are never reused; a discontinued SKU stays in the database for reporting continuity.

Each product has a `list_price` (the published price on the storefront) and a `cost` (our wholesale cost from the supplier or manufacturing partner). Cost is typically 50–70% of list price across categories; the resulting gross margin lands in the 30–50% range.

The `unit_price` recorded on `order_items` is a snapshot of the price at order time. It is not always equal to the current `list_price` because of promotional drift: limited-time discounts, flash sales, and per-customer pricing for loyalty-tier members can push `unit_price` to within ±10% of `list_price` on individual line items. This is why gross-revenue calculations should sum `unit_price * quantity` from `order_items` rather than recomputing from `products.list_price`.

## Pricing Conventions

List prices are denominated in US dollars and use the `numeric(10,2)` SQL type. We do not store prices in cents; all amounts are decimal-rounded to two places at write time.

Promotional discounts on individual line items are recorded in `order_items.discount`. Order-level discounts (loyalty discount, coupon codes) are recorded separately in `orders.discount`. Both reduce gross revenue when computing net revenue; the merchandising team specifically asks that the two be summed when reporting "total discounts" to avoid double-counting.

Shipping fees are recorded in `orders.shipping` and tax in `orders.tax`. Neither contributes to gross revenue — they are pass-through to the carrier and tax authority respectively.

## Category Notes

**Electronics** (`ELE-*`) is our highest-AOV category. Most SKUs are priced between $40 and $200, and the category produces about 35% of total revenue despite being 20% of the SKU count. Electronics carry the longest warranty (12 months) and the strictest return-inspection process — a returned electronics item must include all original cables, manuals, and packaging to pass inspection.

**Apparel** (`APP-*`) has the highest return rate, typically 12–15% on the trailing 90 days, driven by size and fit issues. The merchandising team has a standing instruction to flag any apparel SKU above 18% return rate for fit-guide review.

**Home goods** (`HOM-*`) includes the most diverse product mix: glassware, lighting, textiles, organizers. Glassware has special return-shipping handling — see the Returns Process document.

**Books** (`BOO-*`) is the lowest-margin category, typically 25–35% gross margin. Books are sold at fixed list price; books are never put on flash sale because publisher agreements prohibit it.

**Food** (`FOO-*`) is the smallest category by SKU count but has the highest repeat-purchase rate. Subscription-style food orders (granola, coffee, tea) are handled outside this document — see the Subscription Operations runbook.

## Discontinued Products

A product moves to discontinued status when the supplier confirms it is no longer available. Discontinued products keep their row in the `products` table for revenue-reporting continuity; the `status` flag is set, the SKU is de-listed from the storefront, and the product disappears from all active sort orders.

Discontinued products may still appear on historical orders; reporting queries should not exclude discontinued products from revenue aggregations unless the question is specifically about active catalog performance.
