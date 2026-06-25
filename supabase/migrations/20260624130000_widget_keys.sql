-- US-072 (ADR-0008): the public widget key registry + the not-revoked gate that
-- every key resolution passes through before anything is minted or created.
--
-- WHAT A WIDGET KEY IS (read carefully): `public_key` is NON-SECRET. It is
-- embedded verbatim in the buyer's page JS (the loader <script>, US-083) and is
-- therefore world-readable. It is NOT a credential and grants NO access by
-- itself: it only NAMES which workspace's support bot a widget instance speaks
-- to. The leaked-key blast radius is bounded to "the already-public KB answers"
-- (US-073 frames this), and the hard abuse controls are the rate limit +
-- circuit breaker (US-076/077), NOT the key's secrecy. So unlike
-- `conversation_tokens.token_hash` (US-071, a hashed customer credential) the
-- public_key is stored in the clear — there is nothing to hash.
--
-- TRUST MODEL (this is the ADMIN-MANAGEMENT surface, distinct from the three in
-- CLAUDE.md): widget keys are issued/rotated/revoked by workspace ADMINS in
-- /support/settings (US-090). Managing keys is exactly the administrative action
-- ADR-0002's `workspace_membership.role='admin'` exists for, so — and ONLY here,
-- never in a retrieval/visibility predicate — `role` legitimately enters the RLS
-- below. The public widget never reads this table under a Postgres role at all:
-- key resolution is backend-mediated (the backend reads under the service role,
-- gating on `revoked_at IS NULL`), the same posture US-071 uses for the customer
-- token. So the RLS here protects the ADMIN read/write path; the anonymous
-- public path bypasses it via the service role server-side.

-- widget_keys: one non-secret public key per (workspace, issuance). Multiple keys
-- per workspace are allowed on purpose — rotation is issue-new + revoke-old
-- (no auto-rotation), so an old key lingers (revoked) beside its replacement for
-- audit and to keep already-embedded loaders resolvable until the buyer swaps the
-- snippet. A revoked key blocks NEW conversations (resolution's gate) but never
-- terminates a live one: once a conversation's opaque token (US-071) is minted it
-- is independent of the key, so revocation cannot kill an in-flight chat.
create table public.widget_keys (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  -- Non-secret, embedded in client JS; globally unique so it resolves to exactly
  -- one workspace. The unique constraint is also the resolution lookup index.
  public_key text not null unique,
  label text,
  -- Per-key registered-origin allowlist. US-073 makes this fail-closed
  -- (empty/null => the key is INACTIVE); stored here, enforced there. Defaulted
  -- to the empty array so the column carries no nulls to reason about and a
  -- freshly-issued key is inert until an admin adds an origin.
  allowed_origins text[] not null default '{}'::text[],
  -- Revocation is a one-way flip to a timestamp (never un-revoked); the
  -- resolution gate is `revoked_at IS NULL`. NULL = active.
  revoked_at timestamptz,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

-- Admin listing ("keys for my workspace") and the first-key-issuance check filter
-- by workspace_id; the unique index on public_key already serves resolution.
create index widget_keys_workspace_id_idx
  on public.widget_keys (workspace_id);

alter table public.widget_keys enable row level security;

-- Admin-only management. The EXISTS is the ADR-0002 membership clause WITH the
-- administrative `role='admin'` branch — the one place role belongs (key
-- management IS the administrative action, distinct from the membership-gated
-- queue). A non-admin member of the workspace, and any non-member, sees and
-- writes nothing here through PostgREST. The public resolution path does NOT use
-- these policies; it reads under the service role (RLS bypass) server-side.
create policy widget_keys_select_admin on public.widget_keys
  for select using (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = widget_keys.workspace_id
        and wm.user_id = auth.uid()
        and wm.role = 'admin'
    )
  );

create policy widget_keys_insert_admin on public.widget_keys
  for insert with check (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = widget_keys.workspace_id
        and wm.user_id = auth.uid()
        and wm.role = 'admin'
    )
  );

-- UPDATE is the revoke path (set revoked_at) and the only post-issuance mutation
-- an admin performs. USING + WITH CHECK both pin the row to a workspace the
-- caller admins, so an admin can never move a key into — or flip a key belonging
-- to — a workspace they do not administer.
create policy widget_keys_update_admin on public.widget_keys
  for update using (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = widget_keys.workspace_id
        and wm.user_id = auth.uid()
        and wm.role = 'admin'
    )
  )
  with check (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = widget_keys.workspace_id
        and wm.user_id = auth.uid()
        and wm.role = 'admin'
    )
  );

-- No DELETE policy on purpose: rotation is issue-new + revoke-old, never a hard
-- delete, so a revoked key is retained for audit and to keep already-embedded
-- loaders resolvable-as-revoked. DELETE is therefore deny-by-default to every
-- Postgres role (only the backend service role, bypassing RLS, could prune).

-- One-way revoke latch, enforced in the DB (not just avoided by the endpoint),
-- mirroring the US-067 conversation status machine's "enforced in the DB, not
-- just the service layer" stance. The admin UPDATE policy above intentionally
-- lets an admin freely edit label/allowed_origins, but it does NOT restrict
-- columns — so without this guard a direct PostgREST PATCH could clear
-- `revoked_at` back to NULL and re-activate a key the audit trail records as
-- revoked. This BEFORE-UPDATE trigger makes "never un-revoked" a DB-enforced
-- latch: once `revoked_at` is non-null it is terminal — it can neither be
-- cleared to NULL nor changed to a different timestamp. NULL -> timestamp (the
-- revoke action) and NULL -> NULL (label/origin edits on an active key) stay
-- allowed; only mutating an already-set `revoked_at` is rejected.
create or replace function public._widget_keys_revoke_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if old.revoked_at is not null
     and new.revoked_at is distinct from old.revoked_at then
    raise exception
      'illegal widget_keys revoke change: revoked_at is terminal (cannot be cleared or moved once set)';
  end if;
  return new;
end;
$$;

create or replace trigger widget_keys_revoke_guard
  before update on public.widget_keys
  for each row execute function public._widget_keys_revoke_guard();
