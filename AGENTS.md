# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Two trust models for conversation-shaped tables (do not merge)

There are two parallel chat-message table pairs, on purpose, with **different RLS boundaries**:

- `threads` / `messages` (`supabase/migrations/20260416120000_init_threads_messages.sql`) — the
  knowledge-assistant surface. **Owner-only**: `auth.uid() = user_id`, child delegates to its thread's owner.
  This predicate is the leak-proof core the E4/E6 evals pin; it must stay untouched.
- `conversations` / `conversation_messages` (`supabase/migrations/20260623120000_init_conversations.sql`) —
  the support-widget surface (Epic E). **Workspace-membership**: the ADR-0002 EXISTS-against-
  `workspace_membership` clause (`wm.workspace_id = … and wm.user_id = auth.uid()`), child delegates to the
  parent conversation's workspace. Any member of the workspace can read/claim the queue.

Do NOT collapse these into one table with a `kind` discriminator branching the policy — that puts two
trust models in one predicate (PRD Risk #3 / ADR-0004 reject it). `workspace_membership.role` is
administrative-only (ADR-0002) and must never enter any visibility predicate on these tables.
Cross-workspace zero-leak is pinned by `backend/test_us066_conversations_rls.py` (run via
`python -m backend.test_us066_conversations_rls` against a local Supabase with `DATABASE_URL` set).

## Conversation status machine + derivable deflection (US-067, ADR-0004)

`conversations.status` is a one-way latch `active -> escalated -> resolved` enforced in the DB, not just the service layer.
The `20260623130000_conversation_status_machine.sql` migration installs:

- a CHECK constraint pinning `status in ('active','escalated','resolved')` (added idempotently so it coexists with whatever US-066 defines - exactly once), and
- an idempotent (`create or replace`) `BEFORE INSERT OR UPDATE` trigger (`conversations_status_guard` / `public._conversations_status_guard`) that rejects `escalated -> active` and any `resolved -> *` (resolved is terminal; `resolved -> resolved` no-ops are allowed).

`escalated_at` is a **set-once latch owned entirely by the trigger on both the INSERT and UPDATE paths** - callers never set it. A row born `escalated` is latched `now()` at insert (any other birth status, including `resolved`, gets a null latch); otherwise it is stamped `now()` on the first transition into `escalated` and is preserved verbatim on every later write. An un-latched row always ignores any caller-supplied value, so neither a stray INSERT nor a later UPDATE can plant a timestamp the metric would misread. This is what makes deflection *derivable* instead of a stored `resolved_by_bot`/`resolved_by_human` flag:

- `resolved AND escalated_at IS NULL`     => deflected (bot handled it alone)
- `resolved AND escalated_at IS NOT NULL` => human-handled

Deflection rate from production data (divide-by-zero-guarded):

```sql
select
  count(*) filter (where status = 'resolved' and escalated_at is null)::numeric
  / nullif(count(*) filter (where status = 'resolved'), 0) as deflection_rate
from public.conversations;
```

The runtime "first escalating turn stops the bot pipeline" behaviour is wired separately in US-080; this migration is only the DB-level latch it relies on.
Test: `python -m backend.test_conversation_status_machine` (DB-level, asyncpg as `postgres`; skips cleanly when the local DB / `conversations` table is absent).

## Self-signed Supabase-compatible JWT minting (US-068, ADR-0008)

`backend/supabase_jwt.py:mint_supabase_jwt(sub, ttl_seconds)` is the **single** place the backend issues a Supabase-shaped identity token.
It self-signs a short-lived HS256 JWT with `SUPABASE_JWT_SECRET` (the *same* secret GoTrue signs with), claims `sub` / `role='authenticated'` / `aud='authenticated'` / `iat` / `exp = iat + ttl`, so the token is - to PostgREST and every RLS predicate - indistinguishable from a GoTrue-issued one (`auth.uid()` resolves to `sub`).
This is a new **issuer** beside GoTrue, **not** a new enforcement path - the membership/ACL boundary in the DB is untouched.
Chosen over a GoTrue admin-API session per request (avoids a per-turn round-trip and keeps the service-role key out of the request hot path).

Its only caller is the support bot (US-070): each customer turn mints a ~60s token for `sub = bot_user_id`, calls `match_chunks` as that principal, and discards it.

- `SUPABASE_JWT_SECRET` is a **NEW** env and a **NEW signing surface** (P5 threat-model line): before US-068 the backend held only the anon key (public, non-signing). Whoever holds this secret can forge any identity - server-side only, never embedded client-side. It is **optional**: required only when support is enabled, so the minting helper reads it fail-closed at call time (a knowledge-assistant-only deployment may leave it unset; `main.py` documents it but does not gate startup on it).
- The minted token is **server-side only** and must NEVER reach an HTTP response body, SSE event, or log line bound for the iframe/client.
- The helper mints ONLY for a server-resolved `bot_user_id`, never for a customer- or request-supplied `sub`. PyJWT is now a direct runtime dep (pinned in `requirements.txt`; previously transitive via `supabase`/gotrue).

Test: `python -m backend.test_supabase_jwt` - a unit layer (always runs, no DB/secrets: claim shape, signature-bound-to-secret, TTL validation, fail-closed on missing secret, strict-verifier expiry) plus an integration layer (skips cleanly without local Supabase: a minted token is accepted by PostgREST and resolves `auth.uid()` to `sub`, a token for a different `sub` reads 0 rows, an expired token is rejected with PGRST303).
Note: PostgREST applies a ~30s clock-skew tolerance on `exp`, so the integration expiry check hand-rolls a long-past-`exp` token rather than waiting out a short-TTL minted one.

## Lazy support-bot provisioning + `is_bot` flag (US-069, ADR-0008)

The per-workspace support bot is **not a new content role**. It is an ordinary `auth.users` row plus an ordinary `workspace_membership(role='member')` row, distinguished only by a new boolean flag `is_bot` (`20260624120000_workspace_membership_is_bot.sql`).

- **`is_bot` is a FLAG, not a role.** Like `workspace_membership.role`, it is administrative metadata and must NEVER enter any visibility/retrieval predicate - not `match_chunks`, not `keyword_search`, not the chunks/documents or conversations RLS. The bot is `role='member'`, the administrative-only role grants no content access (ADR-0002), so the bot sees ONLY documents shared to it via `chunk_acl` (share-to-bot), resolved from `auth.uid()` like any other principal. There is **no dedicated `bot` content-role anywhere**. Adding `is_bot` changes the boundary by exactly zero predicates.
- **Exactly one bot per workspace, not per key**, enforced at the DB layer by a partial unique index `workspace_membership_one_bot_per_workspace on workspace_membership (workspace_id) where is_bot`. This is the race guard: two concurrent first-time provisions cannot create two bots - the loser gets a unique violation. Idempotency must NOT be check-then-insert in app code alone; the index is the hard guarantee.
- **Provisioning is `backend/support_bot.py:provision_workspace_bot(workspace_id) -> bot_user_id`** (async). It runs **lazily** (only when a caller invokes it - US-072 will call it on first widget-key issuance; nothing provisions a bot at workspace-creation time) and **idempotently** (a second call returns the existing bot's id, creates no second row). It creates the bot `auth.users` row via the GoTrue admin API (`POST {SUPABASE_URL}/auth/v1/admin/users`, requires `SUPABASE_SERVICE_ROLE_KEY` - resolved fail-closed at call time) then inserts the membership row; on a lost race it drops the orphan `auth.users` row and returns the winner. The returned id populates `conversations.bot_user_id`. The service-role key bypasses RLS - it is server-side only and never logged/returned/sent client-side.
- **Member-management listings MUST exclude the bot** (`where not is_bot`). It is an internal principal, not a human teammate, so the US-008 `/api/workspaces/{id}/members…` surface filters it out. This is a management/presentation contract, not a security boundary. (US-008's member endpoints are not present in this worktree yet; when they land they must add this filter - the migration documents it and the test pins it.) The schema also leaves room for an optional explicit write-deny policy on the bot row (e.g. a RESTRICTIVE `not is_bot` policy); not shipped, since the row is created/owned solely by the service-role path.
- **Optional env `SUPPORT_BOT_EMAIL_DOMAIN`** (default `bots.support.internal`) scopes the bot's internal, non-routable email; the row is admin-created with `email_confirm=true` and no password, so the address never logs in or receives mail.

Test: `python -m backend.test_us069_bot_provisioning` - a unit layer (always runs: workspace_id validation, fail-closed on missing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` before any I/O) plus an integration layer (skips cleanly without local Supabase / the `is_bot` column / a reachable API): provisions twice and asserts exactly ONE `is_bot` row with `role='member'` and a stable returned id (one bot per workspace), no bot before provisioning, member-listing exclusion (`not is_bot`) with the human member as positive control, no `bot` role in the role CHECK, and the partial-unique-index race guard rejecting a second bot insert.
