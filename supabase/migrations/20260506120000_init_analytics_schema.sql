-- US-023: text-to-SQL target schema + read-only role for safe execution.
-- The agent's `query_database` tool generates SELECT statements and runs them
-- as `analytics_readonly`, a role that has no write privileges anywhere.
-- Even if the LLM hallucinates a DROP, the role's lack of permission stops
-- the statement at the database boundary — the SQL safety check in
-- backend/text_to_sql.py is just the first line of defence.
--
-- Production note: Supabase Cloud restricts CREATE ROLE to project owners.
-- In managed environments, run the CREATE ROLE block once via the SQL editor
-- (with a strong password) and re-run the GRANT statements.

create schema if not exists analytics;

-- Validation-test target. PRD US-023 setup says the schema seed is
-- `analytics.orders(id, user_email, total, created_at)` with 100 rows.
create table if not exists analytics.orders (
  id uuid primary key default gen_random_uuid(),
  user_email text not null,
  total numeric(10, 2) not null,
  created_at timestamptz not null default now()
);

-- Idempotent seed: only insert if the table is empty so re-running migrations
-- (or `supabase db reset` in dev) doesn't blow the row count past 100.
insert into analytics.orders (user_email, total, created_at)
select
  'user' || (i % 25 + 1) || '@example.com',
  round((20 + i * 4.37)::numeric, 2),
  now() - (i || ' days')::interval
from generate_series(0, 99) as i
where not exists (select 1 from analytics.orders);

-- Read-only role used by the text-to-SQL tool. The `ANALYTICS_DATABASE_URL`
-- env var must authenticate as this role. Default password is dev-only — set
-- via `alter role analytics_readonly with password '...'` in production.
do $$
begin
  if not exists (select from pg_roles where rolname = 'analytics_readonly') then
    create role analytics_readonly with login password 'analytics_readonly_dev_only';
  end if;
end $$;

grant usage on schema analytics to analytics_readonly;
grant select on all tables in schema analytics to analytics_readonly;
alter default privileges in schema analytics
  grant select on tables to analytics_readonly;

-- Defence in depth: explicitly revoke any default access the role might have
-- to public/auth/storage so a misconfigured search_path still can't cross
-- schemas. The tool-level allowlist (`ALLOWED_SQL_SCHEMAS`) is the primary
-- gate; this is the backstop.
revoke all on schema public from analytics_readonly;
revoke all on all tables in schema public from analytics_readonly;
