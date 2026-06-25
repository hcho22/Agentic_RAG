-- US-071 (ADR-0008, amends ADR-0004): the anonymous customer's opaque
-- per-conversation reconnect credential.
--
-- WHAT THIS TOKEN IS (read carefully): it is NOT a Supabase JWT and is NOT
-- signed with SUPABASE_JWT_SECRET (that secret mints the *bot's* identity token,
-- US-068 — a different mechanism entirely). It is cryptographically-random
-- opaque bytes (`secrets.token_urlsafe`, see backend/conversation_tokens.py),
-- returned in the clear EXACTLY ONCE at conversation creation (only to the
-- iframe) and stored here ONLY as a SHA-256 hash. The raw token never touches
-- the database. This keeps the anonymous customer structurally OFF the Supabase
-- trust surface: continuity comes solely from the iframe-origin-stored token,
-- with NO server-side customer-identity table.
--
-- TRUST MODEL (do not merge with the others): this is a THIRD, narrower boundary
-- beside the two in CLAUDE.md. `threads`/`messages` are owner-only; `conversations`
-- /`conversation_messages` are workspace-membership. This table is neither — it is
-- backend-mediated only: RLS is enabled with NO policies so anon/authenticated are
-- denied wholesale, and the single read path (`resume_conversation`) is a
-- SECURITY DEFINER RPC granted to `service_role` alone. The customer presents the
-- RAW token to the backend over HTTP; the backend hashes it and calls the RPC as
-- the service role. `workspace_membership.role` appears nowhere here (ADR-0002).

-- conversation_tokens: the PRD's recommended shape — one hashed token bound to
-- exactly one conversation, with a sliding 24h expiry. token_hash is the PK (the
-- lookup key); the FK cascades so deleting a conversation drops its tokens.
create table public.conversation_tokens (
  token_hash text primary key,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);

-- Refresh-by-token uses the PK; the purge-on-resolve trigger and any
-- per-conversation maintenance delete by conversation_id, so index it.
create index conversation_tokens_conversation_id_idx
  on public.conversation_tokens (conversation_id);

-- RLS on, NO policies: deny-all to anon/authenticated. Only the backend's
-- service role (RLS bypass) and the SECURITY DEFINER RPC below touch this table.
-- The opaque token is the customer's credential and is resolved server-side; no
-- Supabase principal should ever read a hash directly.
alter table public.conversation_tokens enable row level security;

-- resume_conversation: the SINGLE authoritative read path for the opaque token.
--
-- Given a token HASH (the backend computed it from the raw token the customer
-- presented), resolve it to its bound conversation iff the live invariants hold:
--   * the token row exists,
--   * it is not expired (DB clock, not the app's),
--   * its conversation is not resolved (resolved invalidates the token, US-067).
-- It returns the one conversation the token is bound to. It can never return any
-- other conversation: there is no caller-supplied conversation_id, so a token
-- for X structurally cannot read Y. A miss (missing / expired / resolved) returns
-- zero rows, which the caller reads as "start a fresh conversation".
--
-- p_slide controls the activity refresh: when true (the default), a successful
-- resolution ALSO slides the 24h expiry — every POST /resume is "activity". The
-- read-only GET transcript path passes p_slide=false so a safe/idempotent GET
-- (browser prefetch, link-preview crawler, transparent retry) can never extend a
-- token's lifetime as a side effect of a binding check. The gating/resolution is
-- identical either way; only the UPDATE is conditional.
--
-- SECURITY DEFINER so it reads past `conversations` RLS (the anonymous customer
-- has no JWT), but it is granted to `service_role` ONLY — revoked from PUBLIC so
-- anon/authenticated cannot call it. Token resolution is therefore strictly
-- backend-mediated; combined with 256-bit token entropy the hash is unguessable.
create or replace function public.resume_conversation(
  p_token_hash text,
  p_slide boolean default true
)
returns table (
  id uuid,
  workspace_id uuid,
  status text,
  created_at timestamptz
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_conversation_id uuid;
begin
  if p_token_hash is null or length(p_token_hash) = 0 then
    return;
  end if;

  -- Resolve + gate in one statement, on the DB clock. A resolved conversation's
  -- token is dead even if the row somehow lingers (defense in depth alongside the
  -- purge-on-resolve trigger below).
  select t.conversation_id
    into v_conversation_id
  from public.conversation_tokens t
  join public.conversations c on c.id = t.conversation_id
  where t.token_hash = p_token_hash
    and t.expires_at > now()
    and c.status <> 'resolved';

  if v_conversation_id is null then
    return;  -- missing / expired / resolved -> caller starts a fresh conversation
  end if;

  -- Activity refresh: slide the 24h window on the exact token presented. Skipped
  -- when p_slide is false (the read-only GET transcript path) so a nominally-safe
  -- GET never mutates the token's lifetime.
  if p_slide then
    update public.conversation_tokens
       set expires_at = now() + interval '24 hours'
     where token_hash = p_token_hash;
  end if;

  return query
    select c.id, c.workspace_id, c.status, c.created_at
    from public.conversations c
    where c.id = v_conversation_id;
end;
$$;

-- Backend-only surface: revoke from everyone, grant only to the service role.
-- Supabase's default privileges grant EXECUTE on new public functions to anon
-- and authenticated DIRECTLY (not via PUBLIC), so those grants must be revoked by
-- name as well as PUBLIC — otherwise the anonymous customer's role could call the
-- token-resolution RPC straight against PostgREST.
revoke execute on function public.resume_conversation(text, boolean) from public, anon, authenticated;
grant execute on function public.resume_conversation(text, boolean) to service_role;

-- Invalidate-on-resolve, made literal: when a conversation transitions into
-- 'resolved', delete its tokens so the hash is gone, not merely gated. This both
-- enforces "invalidated on resolve" at the row level and bounds table growth.
-- The status guard (BEFORE UPDATE, US-067) has already validated the transition
-- by the time this AFTER trigger fires.
create or replace function public._conversations_purge_tokens_on_resolve()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.status = 'resolved' and old.status is distinct from 'resolved' then
    delete from public.conversation_tokens where conversation_id = new.id;
  end if;
  return new;
end;
$$;

create or replace trigger conversations_purge_tokens_on_resolve
  after update on public.conversations
  for each row execute function public._conversations_purge_tokens_on_resolve();
