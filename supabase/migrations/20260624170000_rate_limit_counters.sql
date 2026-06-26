-- US-075 (ADR-0008): the Postgres-backed default for the swappable `RateLimiter`
-- seam (backend/rate_limiting.py). This is the abuse-counter store the public
-- widget surface (US-076 per-key/per-session limit, US-077 per-workspace circuit
-- breaker) draws down against. Postgres is the default because it needs NO new
-- infra, is durable across restarts, and is correct cross-instance — the three
-- properties an in-process counter cannot give (it under-counts per instance and
-- resets on restart; rejected in rate_limiting.py per ADR-0008). An optional
-- Redis adapter is the documented scale path; this migration is only what the
-- DEFAULT backend needs.
--
-- TRUST MODEL (do not merge with the others): this is purely BACKEND-MEDIATED
-- internal bookkeeping, the same posture as US-071's conversation_tokens — RLS is
-- enabled with NO policies (anon/authenticated denied wholesale) and the only
-- access path is two SECURITY DEFINER RPCs granted to `service_role` alone. No
-- Supabase principal ever reads or writes a counter directly: the backend calls
-- the RPCs under the service role. `workspace_membership.role` appears nowhere
-- here (ADR-0002 intact) — a rate-limit key is an opaque string the backend
-- composes (e.g. "key:<public_key>", "ip:<addr>", "ws:<workspace_id>"); this
-- store assigns it no meaning and enforces no boundary. The boundary is, as ever,
-- elsewhere; this table only counts.

-- rate_limit_counters: a sliding-window counter kept as fixed-window buckets. The
-- bucket identity is (bucket_key, window_seconds, window_start): the SAME key under
-- two different window sizes never conflates (mirroring the Redis backend, which
-- namespaces buckets by window via `{prefix}:{key}:{window_seconds}`), and the
-- window-relative prune below stays scoped to one (key, window) so a short-window
-- hit can never delete a concurrent longer-window's live bucket. The RPC increments
-- the current bucket and reads the previous one to form the textbook two-window
-- weighted sliding estimate (bounded to <=2 live rows per (key, window), atomic via
-- upsert, no per-hit row growth — unlike a sliding-window *log*, which would amplify
-- writes precisely under the abuse it exists to dampen).
create table public.rate_limit_counters (
  bucket_key text not null,
  -- The window size in seconds: part of the identity so the same key under two
  -- different windows is two independent buckets (Postgres and Redis isolate
  -- by (key, window) identically).
  window_seconds integer not null,
  -- The fixed-window bucket label: floor(epoch/window)*window, as a timestamptz.
  -- It is only a bucket identity; comparisons are consistent regardless of tz.
  window_start timestamptz not null,
  count bigint not null default 0,
  primary key (bucket_key, window_seconds, window_start)
);

-- The composite PK (bucket_key, window_seconds, window_start) already indexes the
-- only access shapes: the point upsert/read of a single bucket and the per-(key,
-- window) prune (`bucket_key = $1 and window_seconds = $2 and window_start < $3`).
-- No extra index needed.

-- RLS on, NO policies: deny-all to anon/authenticated. Only the backend service
-- role (RLS bypass) and the SECURITY DEFINER RPCs below ever touch this table.
alter table public.rate_limit_counters enable row level security;

-- rate_limit_hit: record one hit (cost) against `p_key`'s sliding window and
-- return whether the post-increment estimate is within `p_limit`.
--
-- Algorithm (sliding-window counter, the Cloudflare-style two-bucket weighting):
--   * current bucket  = floor(now/window)*window, incremented atomically here.
--   * previous bucket = current - window, read for its weighted contribution.
--   * estimate = current + previous * (fraction of the previous window still
--     inside the trailing `window`-length sliding interval). At a window boundary
--     the previous bucket counts in full; its weight decays linearly to 0 as we
--     move through the current window. This smooths the fixed-window edge burst
--     (up to 2x the limit at the seam) into a proper sliding bound.
--
-- A blocked hit STILL increments: an attacker who keeps hammering keeps their own
-- window saturated (stays blocked), while a caller who backs off recovers as the
-- window slides — the correct abuse-dampening behavior. The decision and the
-- count are returned together so the caller (US-076) never re-reads (no TOCTOU).
--
-- SECURITY DEFINER so it writes past the deny-all RLS, but granted to
-- `service_role` ONLY (revoked from public/anon/authenticated by name below).
create or replace function public.rate_limit_hit(
  p_key text,
  p_limit integer,
  p_window_seconds integer,
  p_cost integer default 1
)
returns table (
  allowed boolean,
  current_count bigint,
  limit_value integer,
  window_seconds integer
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
-- The RETURNS TABLE column `window_seconds` shares its name with the table column
-- `window_seconds` now in the bucket identity; resolve every ambiguous reference in
-- the body to the column (the OUT field is only ever populated positionally from
-- p_window_seconds, never read by name).
#variable_conflict use_column
declare
  v_epoch       numeric := extract(epoch from now());
  v_win         numeric;
  v_cur_start   numeric;
  v_prev_start  numeric;
  v_prev_weight numeric;
  v_cur         bigint;
  v_prev        bigint;
  v_estimate    numeric;
begin
  if p_key is null or length(p_key) = 0 then
    raise exception 'rate_limit_hit: p_key must be a non-empty string';
  end if;
  if p_window_seconds is null or p_window_seconds <= 0 then
    raise exception 'rate_limit_hit: p_window_seconds must be > 0, got %', p_window_seconds;
  end if;
  if p_cost is null or p_cost < 0 then
    raise exception 'rate_limit_hit: p_cost must be >= 0, got %', p_cost;
  end if;

  v_win         := p_window_seconds::numeric;
  v_cur_start   := floor(v_epoch / v_win) * v_win;
  v_prev_start  := v_cur_start - v_win;
  v_prev_weight := (v_win - (v_epoch - v_cur_start)) / v_win;  -- in (0, 1]

  -- Atomic increment of the current fixed-window bucket.
  insert into public.rate_limit_counters (bucket_key, window_seconds, window_start, count)
  values (p_key, p_window_seconds, to_timestamp(v_cur_start), p_cost)
  on conflict (bucket_key, window_seconds, window_start)
  do update set count = public.rate_limit_counters.count + p_cost
  returning count into v_cur;

  -- Previous bucket for the sliding weight (0 when it never existed / was pruned).
  select c.count into v_prev
  from public.rate_limit_counters c
  where c.bucket_key = p_key
    and c.window_seconds = p_window_seconds
    and c.window_start = to_timestamp(v_prev_start);
  v_prev := coalesce(v_prev, 0);

  -- Opportunistic prune of this (key, window)'s now-irrelevant older buckets so the
  -- table stays bounded to the live window pair per (key, window) without a separate
  -- sweeper. Scoped to this window so a short-window hit never deletes a concurrent
  -- longer-window's live bucket for the same key.
  delete from public.rate_limit_counters c
  where c.bucket_key = p_key
    and c.window_seconds = p_window_seconds
    and c.window_start < to_timestamp(v_prev_start);

  v_estimate := v_cur::numeric + (v_prev::numeric * v_prev_weight);

  return query select
    (ceil(v_estimate) <= p_limit::numeric),
    ceil(v_estimate)::bigint,
    p_limit,
    p_window_seconds;
end;
$$;

-- rate_limit_count: read-only peek of the current sliding-window estimate for
-- `p_key` WITHOUT recording a hit (introspection / the US-075 durability check).
-- Same weighting as rate_limit_hit; never mutates, never prunes.
create or replace function public.rate_limit_count(
  p_key text,
  p_window_seconds integer
)
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_epoch       numeric := extract(epoch from now());
  v_win         numeric;
  v_cur_start   numeric;
  v_prev_start  numeric;
  v_prev_weight numeric;
  v_cur         bigint;
  v_prev        bigint;
begin
  if p_key is null or length(p_key) = 0 then
    raise exception 'rate_limit_count: p_key must be a non-empty string';
  end if;
  if p_window_seconds is null or p_window_seconds <= 0 then
    raise exception 'rate_limit_count: p_window_seconds must be > 0, got %', p_window_seconds;
  end if;

  v_win         := p_window_seconds::numeric;
  v_cur_start   := floor(v_epoch / v_win) * v_win;
  v_prev_start  := v_cur_start - v_win;
  v_prev_weight := (v_win - (v_epoch - v_cur_start)) / v_win;

  select c.count into v_cur
  from public.rate_limit_counters c
  where c.bucket_key = p_key
    and c.window_seconds = p_window_seconds
    and c.window_start = to_timestamp(v_cur_start);
  v_cur := coalesce(v_cur, 0);

  select c.count into v_prev
  from public.rate_limit_counters c
  where c.bucket_key = p_key
    and c.window_seconds = p_window_seconds
    and c.window_start = to_timestamp(v_prev_start);
  v_prev := coalesce(v_prev, 0);

  return ceil(v_cur::numeric + (v_prev::numeric * v_prev_weight))::bigint;
end;
$$;

-- Backend-only surface. Supabase grants EXECUTE on new public functions to anon
-- and authenticated DIRECTLY (not via PUBLIC), so revoke by name as well as from
-- PUBLIC — otherwise an anonymous or authenticated principal could draw down /
-- read the abuse counters straight against PostgREST. Then grant to the service
-- role alone, exactly as US-071's resume_conversation does.
revoke execute on function public.rate_limit_hit(text, integer, integer, integer)
  from public, anon, authenticated;
grant execute on function public.rate_limit_hit(text, integer, integer, integer)
  to service_role;

revoke execute on function public.rate_limit_count(text, integer)
  from public, anon, authenticated;
grant execute on function public.rate_limit_count(text, integer)
  to service_role;
