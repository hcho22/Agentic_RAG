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
