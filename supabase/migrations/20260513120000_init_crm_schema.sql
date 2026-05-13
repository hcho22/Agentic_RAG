-- US-029: Module 9 CRM schema for semantic-layer / structured-RAG target.
-- Five tables host the ambiguity that the planner + semantic layer resolves:
--   orders carries five revenue-flavored columns (subtotal, tax, shipping,
--   discount, total) and refunds.amount reduces net revenue. Multiple date
--   columns (created_at, paid_at, shipped_at on orders; created_at,
--   first_order_at, last_order_at on customers) host time-grain ambiguity.
--
-- Read by the new `crm_readonly` role. Seed data lives in
-- supabase/seed/crm_seed.py (deterministic faker) per the PRD — the migration
-- only creates structure so re-applying it doesn't compound rows.

create schema if not exists crm;

create table if not exists crm.customers (
  id            uuid primary key default gen_random_uuid(),
  email         text not null unique,
  name          text not null,
  country       text not null,
  segment       text not null,
  created_at    timestamptz not null default now(),
  first_order_at timestamptz,
  last_order_at  timestamptz
);

create table if not exists crm.products (
  id          uuid primary key default gen_random_uuid(),
  sku         text not null unique,
  name        text not null,
  category    text not null,
  list_price  numeric(10, 2) not null,
  cost        numeric(10, 2) not null,
  created_at  timestamptz not null default now()
);

create table if not exists crm.orders (
  id           uuid primary key default gen_random_uuid(),
  customer_id  uuid not null references crm.customers(id) on delete cascade,
  status       text not null,
  subtotal     numeric(10, 2) not null,
  tax          numeric(10, 2) not null default 0,
  shipping     numeric(10, 2) not null default 0,
  discount     numeric(10, 2) not null default 0,
  total        numeric(10, 2) not null,
  created_at   timestamptz not null default now(),
  paid_at      timestamptz,
  shipped_at   timestamptz
);

create index if not exists orders_customer_id_idx on crm.orders(customer_id);
create index if not exists orders_created_at_idx on crm.orders(created_at);
create index if not exists orders_paid_at_idx on crm.orders(paid_at);

create table if not exists crm.order_items (
  id          uuid primary key default gen_random_uuid(),
  order_id    uuid not null references crm.orders(id) on delete cascade,
  product_id  uuid not null references crm.products(id) on delete restrict,
  quantity    integer not null check (quantity > 0),
  unit_price  numeric(10, 2) not null,
  discount    numeric(10, 2) not null default 0,
  line_total  numeric(10, 2) not null
);

create index if not exists order_items_order_id_idx on crm.order_items(order_id);
create index if not exists order_items_product_id_idx on crm.order_items(product_id);

create table if not exists crm.refunds (
  id          uuid primary key default gen_random_uuid(),
  order_id    uuid not null references crm.orders(id) on delete cascade,
  amount      numeric(10, 2) not null check (amount >= 0),
  reason      text,
  created_at  timestamptz not null default now()
);

create index if not exists refunds_order_id_idx on crm.refunds(order_id);
create index if not exists refunds_created_at_idx on crm.refunds(created_at);

-- Read-only role for the structured-RAG path. The planner compiles SQL from
-- the semantic layer and executes it as `crm_readonly` — even a planner bug
-- that emits a DELETE would hit a permission error before touching data.
-- Mirrors the analytics_readonly pattern (see 20260506120000).
do $$
begin
  if not exists (select from pg_roles where rolname = 'crm_readonly') then
    create role crm_readonly with login password 'crm_readonly_dev_only';
  end if;
end $$;

grant usage on schema crm to crm_readonly;
grant select on all tables in schema crm to crm_readonly;
alter default privileges in schema crm
  grant select on tables to crm_readonly;

-- Defence in depth: keep the role out of every other schema so a misconfigured
-- search_path can't smuggle queries past the allowlist. Same pattern as
-- analytics_readonly.
revoke all on schema public from crm_readonly;
revoke all on all tables in schema public from crm_readonly;
