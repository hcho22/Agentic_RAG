"""US-029: deterministic seed for the `crm` schema.

Re-runnable. Truncates the five tables before seeding so the row counts
stay at the targets (~200 customers, ~50 products, ~1000 orders,
~3000 order_items, ~100 refunds) regardless of how many times the script
runs. Uses a fixed RNG seed so the rows are byte-identical across runs —
the eval harness in US-031 relies on stable gold values.

Connects via `CRM_SEED_DATABASE_URL` (preferred) or falls back to
`DATABASE_URL`. Both must point at a writable role (Supabase local default
is `postgresql://postgres:postgres@localhost:54322/postgres`); the
`crm_readonly` role used by the agent at query time cannot write.

Run:
    python -m db_seed.crm_seed

Note: this module lives at the repo root rather than under `supabase/`
because the local `supabase/` directory (Supabase CLI workspace) collides
with the installed `supabase` PyPI package — `python -m supabase.seed.*`
fails with ``ModuleNotFoundError: No module named 'supabase.seed'``.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg


SEED = 20260513  # Anchor the RNG so reruns produce identical rows.

NUM_CUSTOMERS = 200
NUM_PRODUCTS = 50
NUM_ORDERS = 1000
AVG_ITEMS_PER_ORDER = 3  # ~3000 items total
REFUND_RATE = 0.10  # ~100 refunds

COUNTRIES = ("US", "UK", "DE", "FR", "JP")
SEGMENTS = ("enterprise", "smb", "consumer")
CATEGORIES = ("electronics", "apparel", "home", "books", "food")
ORDER_STATUSES = ("paid", "shipped", "refunded", "cancelled", "pending")
# Weighted so most orders are paid/shipped; refunded/cancelled/pending are
# rarer. Refunded specifically gets its own non-zero base rate independent of
# the REFUND_RATE — REFUND_RATE drives whether a refund *row* is created.
ORDER_STATUS_WEIGHTS = (0.45, 0.30, 0.10, 0.08, 0.07)

FIRST_NAMES = (
    "Aiko", "Ben", "Chen", "Dara", "Eva", "Felix", "Grace", "Hugo",
    "Ines", "Jin", "Kai", "Lina", "Maya", "Noah", "Omar", "Priya",
    "Quincy", "Ravi", "Sara", "Tomas", "Uma", "Viktor", "Wen", "Xavi",
    "Yuki", "Zara",
)
LAST_NAMES = (
    "Adler", "Brun", "Costa", "Diaz", "Eom", "Fischer", "Gauthier",
    "Hassan", "Ito", "Joshi", "Kim", "Laurent", "Mori", "Nguyen",
    "Ostrom", "Park", "Quinn", "Reyes", "Sato", "Tanaka", "Ueno",
    "Vogt", "Wang", "Xu", "Yamada", "Zheng",
)
PRODUCT_ADJ = (
    "Compact", "Pro", "Lite", "Elite", "Classic", "Modern", "Ultra",
    "Deluxe", "Eco", "Smart", "Quick", "Heritage", "Studio",
)
PRODUCT_NOUNS = {
    "electronics": ("Headphones", "Speaker", "Charger", "Cable", "Adapter",
                    "Hub", "Drive", "Camera", "Light", "Tracker"),
    "apparel":     ("Tee", "Hoodie", "Jacket", "Pants", "Cap",
                    "Socks", "Scarf", "Belt", "Vest", "Gloves"),
    "home":        ("Mug", "Lamp", "Pillow", "Throw", "Tray",
                    "Vase", "Coaster", "Hook", "Organizer", "Frame"),
    "books":       ("Notebook", "Journal", "Planner", "Atlas", "Guide",
                    "Cookbook", "Novel", "Anthology", "Reader", "Manual"),
    "food":        ("Granola", "Coffee", "Tea", "Honey", "Jam",
                    "Snack", "Spice", "Sauce", "Bar", "Chocolate"),
}
REFUND_REASONS = (
    "damaged in transit",
    "wrong item shipped",
    "customer dissatisfied",
    "duplicate order",
    "delivery delay",
    "product defect",
)


def _round_money(value: float) -> Decimal:
    """Match the numeric(10,2) columns; avoid float drift in totals."""
    return Decimal(f"{value:.2f}")


async def _connect() -> asyncpg.Connection:
    url = (
        os.environ.get("CRM_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "set CRM_SEED_DATABASE_URL (or DATABASE_URL) to a writable "
            "Postgres connection string"
        )
    return await asyncpg.connect(url)


def _gen_customers(rng: random.Random, now: datetime) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for i in range(NUM_CUSTOMERS):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        # i is in the email so 200 rows are guaranteed unique even if
        # first+last collide.
        email = f"{first.lower()}.{last.lower()}.{i:03d}@example.com"
        country = rng.choice(COUNTRIES)
        segment = rng.choice(SEGMENTS)
        created = now - timedelta(days=rng.randint(30, 730))
        rows.append((
            uuid.UUID(int=rng.getrandbits(128)),
            email,
            f"{first} {last}",
            country,
            segment,
            created,
            None,  # first_order_at — backfilled after orders seed
            None,  # last_order_at  — backfilled after orders seed
        ))
    return rows


def _gen_products(rng: random.Random, now: datetime) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for i in range(NUM_PRODUCTS):
        category = CATEGORIES[i % len(CATEGORIES)]
        noun = rng.choice(PRODUCT_NOUNS[category])
        adj = rng.choice(PRODUCT_ADJ)
        name = f"{adj} {noun}"
        sku = f"{category[:3].upper()}-{i:04d}"
        # list_price drives unit_price below; cost is 50-70% of list so
        # gross margin lands at 30-50% — a realistic retail range.
        list_price = round(rng.uniform(9.99, 199.99), 2)
        cost = round(list_price * rng.uniform(0.50, 0.70), 2)
        created = now - timedelta(days=rng.randint(60, 900))
        rows.append((
            uuid.UUID(int=rng.getrandbits(128)),
            sku,
            name,
            category,
            _round_money(list_price),
            _round_money(cost),
            created,
        ))
    return rows


def _gen_orders_and_items(
    rng: random.Random,
    now: datetime,
    customers: list[tuple[Any, ...]],
    products: list[tuple[Any, ...]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    orders: list[tuple[Any, ...]] = []
    items: list[tuple[Any, ...]] = []
    for i in range(NUM_ORDERS):
        customer = rng.choice(customers)
        customer_id = customer[0]
        status = rng.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS, k=1)[0]
        created = now - timedelta(
            days=rng.randint(0, 365),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        # Cancelled orders never paid/shipped; pending never shipped. The
        # rest get a paid_at within 0-3 days and shipped_at 1-7 days after
        # that. This is the time-grain bait for the doc: "when did this
        # order happen?" has three legitimate answers.
        paid_at = (
            created + timedelta(hours=rng.randint(1, 72))
            if status not in ("cancelled", "pending") else None
        )
        shipped_at = (
            paid_at + timedelta(days=rng.randint(1, 7))
            if paid_at is not None and status not in ("paid", "cancelled", "pending")
            else None
        )

        n_items = max(1, int(rng.gauss(AVG_ITEMS_PER_ORDER, 1.0)))
        n_items = min(n_items, 8)
        order_id = uuid.UUID(int=rng.getrandbits(128))

        subtotal_f = 0.0
        for _ in range(n_items):
            product = rng.choice(products)
            product_id = product[0]
            list_price = float(product[4])
            # Snapshot unit_price: small +/- 10% drift to simulate price
            # changes at order time. This is metric-ambiguity bait — it
            # means SUM(unit_price * quantity) and SUM(orders.subtotal) can
            # legitimately diverge from SUM(products.list_price * quantity).
            unit_price = round(list_price * rng.uniform(0.90, 1.10), 2)
            quantity = rng.randint(1, 4)
            item_discount = round(
                unit_price * quantity * rng.uniform(0.0, 0.15),
                2,
            ) if rng.random() < 0.25 else 0.0
            line_total = round(unit_price * quantity - item_discount, 2)
            subtotal_f += line_total
            items.append((
                uuid.UUID(int=rng.getrandbits(128)),
                order_id,
                product_id,
                quantity,
                _round_money(unit_price),
                _round_money(item_discount),
                _round_money(line_total),
            ))

        tax_f = round(subtotal_f * 0.08, 2)
        shipping_f = (
            round(rng.uniform(0.0, 15.0), 2)
            if rng.random() < 0.7 else 0.0
        )
        order_discount_f = (
            round(subtotal_f * rng.uniform(0.05, 0.20), 2)
            if rng.random() < 0.15 else 0.0
        )
        total_f = round(subtotal_f + tax_f + shipping_f - order_discount_f, 2)

        orders.append((
            order_id,
            customer_id,
            status,
            _round_money(subtotal_f),
            _round_money(tax_f),
            _round_money(shipping_f),
            _round_money(order_discount_f),
            _round_money(total_f),
            created,
            paid_at,
            shipped_at,
        ))
    return orders, items


def _gen_refunds(
    rng: random.Random,
    orders: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    target_count = int(NUM_ORDERS * REFUND_RATE)
    # Prefer refunding orders that were actually paid — refunding a cancelled
    # order would be nonsensical and hurts the eval's realism.
    eligible = [o for o in orders if o[2] in ("paid", "shipped", "refunded")]
    if len(eligible) < target_count:
        eligible = orders  # degrade gracefully on tiny seeds
    chosen = rng.sample(eligible, target_count)
    for order in chosen:
        order_id = order[0]
        order_total = float(order[7])
        # Refund is some fraction of the order total. Full refunds are
        # common; partial refunds are the interesting bait for net_revenue.
        if rng.random() < 0.6:
            amount = order_total  # full refund
        else:
            amount = round(order_total * rng.uniform(0.10, 0.80), 2)
        order_created = order[8]
        refund_at = order_created + timedelta(days=rng.randint(1, 45))
        rows.append((
            uuid.UUID(int=rng.getrandbits(128)),
            order_id,
            _round_money(amount),
            rng.choice(REFUND_REASONS),
            refund_at,
        ))
    return rows


async def _truncate(conn: asyncpg.Connection) -> None:
    # CASCADE so the FK chain (refunds, order_items, orders depend on
    # customers/products) collapses in one statement.
    await conn.execute(
        "truncate table crm.refunds, crm.order_items, crm.orders, "
        "crm.products, crm.customers restart identity cascade"
    )


async def _backfill_customer_order_dates(conn: asyncpg.Connection) -> None:
    """Populate customers.first_order_at / last_order_at from the orders table.
    These columns exist so the planner can map "active customer" to a
    canonical column — they have to be in sync with the actual order data."""
    await conn.execute(
        """
        update crm.customers c
           set first_order_at = sub.first_order,
               last_order_at  = sub.last_order
          from (
            select customer_id,
                   min(created_at) as first_order,
                   max(created_at) as last_order
              from crm.orders
             group by customer_id
          ) sub
         where c.id = sub.customer_id
        """
    )


async def seed() -> dict[str, int]:
    """Truncate + reseed the crm.* tables. Returns the per-table row counts."""
    rng = random.Random(SEED)
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)

    customers = _gen_customers(rng, now)
    products = _gen_products(rng, now)
    orders, items = _gen_orders_and_items(rng, now, customers, products)
    refunds = _gen_refunds(rng, orders)

    conn = await _connect()
    try:
        await _truncate(conn)
        await conn.copy_records_to_table(
            "customers",
            schema_name="crm",
            records=customers,
            columns=[
                "id", "email", "name", "country", "segment",
                "created_at", "first_order_at", "last_order_at",
            ],
        )
        await conn.copy_records_to_table(
            "products",
            schema_name="crm",
            records=products,
            columns=[
                "id", "sku", "name", "category",
                "list_price", "cost", "created_at",
            ],
        )
        await conn.copy_records_to_table(
            "orders",
            schema_name="crm",
            records=orders,
            columns=[
                "id", "customer_id", "status",
                "subtotal", "tax", "shipping", "discount", "total",
                "created_at", "paid_at", "shipped_at",
            ],
        )
        await conn.copy_records_to_table(
            "order_items",
            schema_name="crm",
            records=items,
            columns=[
                "id", "order_id", "product_id",
                "quantity", "unit_price", "discount", "line_total",
            ],
        )
        await conn.copy_records_to_table(
            "refunds",
            schema_name="crm",
            records=refunds,
            columns=["id", "order_id", "amount", "reason", "created_at"],
        )
        await _backfill_customer_order_dates(conn)
    finally:
        await conn.close()

    return {
        "customers": len(customers),
        "products": len(products),
        "orders": len(orders),
        "order_items": len(items),
        "refunds": len(refunds),
    }


def main() -> None:
    counts = asyncio.run(seed())
    print("crm seed complete:")
    for table, n in counts.items():
        print(f"  {table}: {n}")


if __name__ == "__main__":
    main()
