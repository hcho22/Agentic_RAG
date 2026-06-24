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
