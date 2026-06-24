-- US-067: Conversation status machine + escalation latch + derivable deflection.
--
-- Builds the one-way status latch on top of the `conversations` table created by
-- US-066's `init_conversations` migration. The model (ADR-0004) is:
--
--     active --> escalated --> resolved      (escalated is optional)
--     active ------------------> resolved
--
-- and the key insight is that we do NOT split `resolved` into
-- `resolved_by_bot` / `resolved_by_human`. Instead deflection is *derived* from
-- whether the conversation was ever escalated, recorded by the set-once
-- `escalated_at` latch:
--
--     resolved AND escalated_at IS NULL      => deflected   (bot handled it alone)
--     resolved AND escalated_at IS NOT NULL  => human-handled
--
-- This migration makes the DB the source of truth for those invariants so a
-- buggy or malicious writer cannot de-escalate, resurrect a resolved
-- conversation, or rewrite the latch. `escalated_at` is trigger-owned on BOTH
-- the create (INSERT) and the update path, so neither a stray INSERT nor a later
-- UPDATE can plant a timestamp the deflection metric would misread. (The "first
-- escalating turn stops the bot pipeline" behaviour is a runtime concern wired
-- in US-080; this migration only provides the DB-level latch it relies on.)

-- 1. Allowed status set. US-066 defaults `status` to 'active' but does not
--    constrain the value set; this hardens it. Added idempotently so that if
--    US-066 (or a future migration) already constrains `status` to the same
--    three values the constraint exists exactly once, never duplicated.
do $$
begin
  if not exists (
    select 1
    from pg_constraint c
    join pg_class t on t.oid = c.conrelid
    join pg_namespace n on n.oid = t.relnamespace
    where n.nspname = 'public'
      and t.relname = 'conversations'
      and c.contype = 'c'
      and pg_get_constraintdef(c.oid) ilike '%status%'
      and pg_get_constraintdef(c.oid) ilike '%active%'
      and pg_get_constraintdef(c.oid) ilike '%escalated%'
      and pg_get_constraintdef(c.oid) ilike '%resolved%'
  ) then
    alter table public.conversations
      add constraint conversations_status_check
      check (status in ('active', 'escalated', 'resolved'));
  end if;
end$$;

-- 2. Transition guard + escalation latch.
--
-- A BEFORE INSERT OR UPDATE trigger (rather than service-layer-only checks) so
-- the invariant holds for every writer - bot pipeline, support agent, admin SQL
-- - on both the create and the update path. The trigger is the *sole author* of
-- `escalated_at` on BOTH paths: callers never set it directly, which keeps the
-- deflection derivation trustworthy (no INSERT can plant a stray latch either).
--
-- Rules enforced:
--   * INSERT: escalated_at forced from the birth status - now() iff the row is
--             born 'escalated', else null (any caller-supplied value ignored)
--   * escalated -> active   rejected (no de-escalation)
--   * resolved  -> anything  rejected (resolved is terminal); resolved ->
--                            resolved is a no-op same-status write and allowed
--   * active -> escalated, active -> resolved, escalated -> resolved allowed
--   * same-status writes (active/escalated/resolved -> itself) allowed
--   * escalated_at is stamped now() on the FIRST entry into 'escalated' and is
--     never changed or cleared by any later write (set-once latch)
create or replace function public._conversations_status_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  -- INSERT: escalated_at is trigger-owned on the create path too. A row born
  -- 'escalated' is latched now(); any other birth status (including 'resolved')
  -- gets a null latch, ignoring any caller-supplied escalated_at. This keeps the
  -- metric derivable from the create path - an inserted 'resolved' row that was
  -- never escalated correctly counts as deflected.
  if tg_op = 'INSERT' then
    if new.status = 'escalated' then
      new.escalated_at := now();
    else
      new.escalated_at := null;
    end if;
    return new;
  end if;

  -- UPDATE: escalated_at is owned entirely by this trigger.
  if old.escalated_at is not null then
    -- Already latched: preserve it verbatim regardless of the incoming row.
    -- This is the set-once guarantee - the latch can never be moved or cleared.
    new.escalated_at := old.escalated_at;
  else
    -- Not yet latched: ignore any caller-supplied value so the latch can only
    -- be set by the escalate transition below (keeps deflection derivable -
    -- an `active`/deflected `resolved` row can never carry a stray timestamp).
    new.escalated_at := null;
  end if;

  if new.status is distinct from old.status then
    -- Backward transition: no de-escalation.
    if old.status = 'escalated' and new.status = 'active' then
      raise exception 'illegal conversation status transition: escalated -> active (no de-escalation)';
    end if;

    -- Terminal state: resolved is a sink, no transition leaves it.
    if old.status = 'resolved' then
      raise exception 'illegal conversation status transition: resolved -> % (resolved is terminal)', new.status;
    end if;

    -- Latch on the first transition into escalated.
    if new.status = 'escalated' and new.escalated_at is null then
      new.escalated_at := now();
    end if;
  end if;

  return new;
end;
$$;

create or replace trigger conversations_status_guard
  before insert or update on public.conversations
  for each row execute function public._conversations_status_guard();

-- 3. Deflection rate from production data (ADR-0004). Deflection is derived,
--    never stored: a resolved conversation that was never escalated was handled
--    by the bot alone. Guarded against divide-by-zero (NULL when no resolved
--    conversations exist yet):
--
--      select
--        count(*) filter (where status = 'resolved' and escalated_at is null)::numeric
--        / nullif(count(*) filter (where status = 'resolved'), 0) as deflection_rate
--      from public.conversations;
--
--    Scope it per workspace / time window by adding the usual WHERE clause, e.g.
--    `where workspace_id = $1 and created_at >= now() - interval '30 days'`.
