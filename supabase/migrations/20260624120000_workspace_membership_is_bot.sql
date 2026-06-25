-- US-069 (ADR-0008, layered on ADR-0002): the per-workspace support bot.
--
-- The support bot is NOT a new content role. It is an ordinary `auth.users` row
-- plus an ordinary `workspace_membership(role='member')` row, distinguished only
-- by a new boolean FLAG, `is_bot`. The bot is provisioned lazily and idempotently
-- by `backend/support_bot.py:provision_workspace_bot` when support is first
-- enabled (US-072's first widget-key issuance); a knowledge-assistant-only
-- deployment never provisions one. The provisioned bot's user id is what
-- populates `conversations.bot_user_id` (see 20260623120000_init_conversations.sql).
--
-- `is_bot` IS A FLAG, NOT A ROLE. Like `workspace_membership.role`, it is
-- ADMINISTRATIVE METADATA ONLY and must NEVER appear in any visibility /
-- retrieval predicate — not in `match_chunks`, not in `keyword_search`, not in
-- the chunks/documents RLS, not in the conversations RLS. The bot is
-- `role='member'`, and the administrative-only role grants NO content access
-- (ADR-0002), so the bot sees ONLY documents shared to it through `chunk_acl`
-- (share-to-bot). There is deliberately NO dedicated `bot` content-role anywhere;
-- the bot's reach is exactly its owner-OR-ACL set, resolved from `auth.uid()`
-- like any other principal. Adding `is_bot` here changes the boundary by exactly
-- zero predicates.
--
-- MEMBER-MANAGEMENT LISTINGS MUST EXCLUDE THE BOT. The bot is an internal
-- principal, not a human teammate, so any admin member-list query/endpoint (the
-- US-008 `/api/workspaces/{id}/members…` surface) MUST filter `where is_bot =
-- false` (equivalently `where not is_bot`). This is a presentation/management
-- contract, NOT a security boundary — it keeps the bot out of "manage your team"
-- views. The partial unique index below also makes that filter index-friendly.
--
-- WRITE-DENY ROOM (intentionally not shipped here). The schema leaves room for an
-- optional explicit policy that denies human callers from mutating the bot's
-- membership row (e.g. a RESTRICTIVE policy `for update/delete using (not
-- is_bot)` on workspace_membership, or routing bot writes through the service
-- role only). It is not added now because the bot row is created/owned solely by
-- the service-role provisioning path and US-008's mutations are admin-gated; the
-- flag and index here are what such a policy would key off if a deployment wants
-- it. (ADR-0002's role-is-administrative rule applies to is_bot identically.)

-- 1. The flag. Defaults false so every existing membership row (and every human
--    member added later) is a non-bot, leaving the legacy corpus and all
--    retrieval predicates completely unchanged. IF NOT EXISTS keeps the migration
--    re-runnable against the shared local DB.
alter table public.workspace_membership
  add column if not exists is_bot boolean not null default false;

-- 2. EXACTLY ONE BOT PER WORKSPACE, enforced at the DB layer (not check-then-
--    insert in app code) so two concurrent first-time provisions — e.g. two
--    admins enabling support at once — cannot create two bots. The partial unique
--    index covers only the is_bot=true rows: it permits unlimited human members
--    per workspace while admitting at most one bot membership per workspace. The
--    provisioning primitive relies on this as its serialization point: the loser
--    of a race gets a unique violation, catches it, and returns the winner's bot.
--    "One bot per WORKSPACE, not per key" (US-069) is exactly this invariant.
create unique index if not exists workspace_membership_one_bot_per_workspace
  on public.workspace_membership (workspace_id)
  where is_bot;

comment on column public.workspace_membership.is_bot is
  'US-069/ADR-0008: marks the per-workspace support bot membership. Administrative '
  'metadata ONLY (like role) — never in any visibility/retrieval predicate. The bot '
  'is role=member; it sees only share-to-bot documents via chunk_acl. Exactly one '
  'is_bot row per workspace (partial unique index). Member-management listings must '
  'filter is_bot = false.';
