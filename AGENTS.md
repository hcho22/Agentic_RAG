# Project agent memory

This file is the project's committed home for durable, project-intrinsic agent knowledge: the cross-cutting invariants that prevent mistakes.
It is deliberately lean (context engineering: carry the high-signal rules, not the history).
The full per-story detail, rationale, and `Test:` commands for the Epic-E support widget + Phase-2 eval-gate work (US-066 through US-093) live in `docs/support-widget-internals.md`, the merged PRs (#51-#70), and git history.

- Add durable project-specific notes here as they are discovered through real work; put per-story specifics in `docs/support-widget-internals.md`.

## Core invariants (do not violate)

These are distilled from the whole Epic-E surface. If a change appears to require breaking one, stop and reconsider - each is load-bearing for tenant isolation or the deflection metric.

1. **`role` and `is_bot` are administrative-only flags - NEVER in a retrieval/visibility predicate** (ADR-0002). `workspace_membership.role` and `is_bot` must not appear in `match_chunks` / `keyword_search` / chunks / conversations RLS, or any visibility gate. The bot is `role='member'` and sees only what `chunk_acl` shares to it. The ONE legitimate exception: `role='admin'` gates the `widget_keys` admin-management RLS (issue/rotate/revoke is the administrative action the role exists for).

2. **Four trust boundaries, kept apart** - do not merge them: (a) owner-only `threads`/`messages`; (b) workspace-membership `conversations`/`conversation_messages` (see next section); (c) the opaque per-conversation customer token (backend-mediated, deny-all RLS); (d) the non-secret widget public key (names a workspace's bot, grants nothing). Never collapse the two conversation-table models into one `kind`-discriminated table.

3. **Secrets are server-side only - never an SSE event, response body, or log line.** This covers the minted bot JWT (US-068), the `SUPABASE_SERVICE_ROLE_KEY`, and the raw customer token (US-071, returned exactly once in the `X-Conversation-Token` response header, never in an SSE event). The widget `public_key` is the only non-secret and may be embedded client-side.

4. **fail-CLOSED where a miss leaks; fail-SOFT where a miss is cosmetic.** Auth / escalation / grant paths fail closed (e.g. `_reject_if_bot_principal` raises rather than risk a silent bot share; a bad token → 401/404; a botless/degraded deflection turn → generic deferral). Presentation filters fail soft (e.g. `get_shares` bot-row filter falls back to an unfiltered list rather than 500 the read). Never invert these.

5. **One-way latches are DB-enforced, not just service-layer.** `conversations.status` moves only `active → escalated → resolved` (trigger-guarded; resolved is terminal). `widget_keys.revoked_at` cannot be cleared or moved (trigger). Resolving a conversation purges its customer tokens (AFTER trigger). Do not re-implement these as app-layer checks.

6. **`escalated_at` is trigger-owned - callers NEVER plant it.** The US-067 status trigger stamps it on the first transition into `escalated` and preserves it verbatim thereafter. This is what keeps deflection **derivable, not stored**: `resolved AND escalated_at IS NULL` ⇒ deflected (bot alone); `resolved AND escalated_at IS NOT NULL` ⇒ human-handled. Never add a `resolved_by_bot`/`resolved_by_human` column.

7. **`filter_workspace_id` is a non-security narrowing filter, never the boundary.** On `match_chunks`/`keyword_search` it can only subtract within what the `auth.uid()` membership clause already allows; `null` is a no-op. The trust boundary is always the membership EXISTS clause resolved from the principal's JWT, never this param.

8. **Escalate-vs-answer is deterministic control flow, never a model `escalate()` tool** (ADR-0003). The deflection pipeline decides; a tripped circuit breaker or a weak-retrieval/unfaithful draft latches escalation. A degraded/transient failure defers this turn but does NOT latch (may recover); only a deliberate escalate latches the bot silent forever.

9. **Ship the seam before the call-site.** New cross-cutting mechanisms land as an abstract seam with its own test, then a later story wires the live call-site (US-075→076, US-077→079, US-082→081, US-084→081). Follow this pattern for new infrastructure.

10. **Support is optional and fails closed at the boundary.** With no `SUPABASE_SERVICE_ROLE_KEY`, every `/widget/*` route 503s (inert, never unprotected) and the rate limiter/breaker are a clean no-op. With no `SUPABASE_JWT_SECRET`, the bot degrades to the generic deferral rather than answering. A knowledge-assistant-only deploy leaves both unset and is unaffected.

## Two trust models for conversation-shaped tables (do not merge)

Two parallel chat-message table pairs exist on purpose, with **different RLS boundaries**:

- `threads` / `messages` (`supabase/migrations/20260416120000_init_threads_messages.sql`) - the knowledge-assistant surface. **Owner-only**: `auth.uid() = user_id`, child delegates to its thread's owner. This is the leak-proof core the E4/E6 evals pin; it must stay untouched.
- `conversations` / `conversation_messages` (`supabase/migrations/20260623120000_init_conversations.sql`) - the support-widget surface (Epic E). **Workspace-membership**: the ADR-0002 EXISTS-against-`workspace_membership` clause (`wm.workspace_id = … and wm.user_id = auth.uid()`), child delegates to the parent conversation's workspace. Any member of the workspace can read/claim the queue.

Do NOT collapse these into one table with a `kind` discriminator branching the policy - that puts two trust models in one predicate (PRD Risk #3 / ADR-0004 reject it). Cross-workspace zero-leak is pinned by `backend/test_us066_conversations_rls.py`. Per-table specifics are in `docs/support-widget-internals.md`.

## How to test

- Backend story tests are `python -m backend.test_usXXX_*` (plus `test_conversation_status_machine`, `test_supabase_jwt`, `test_au4_auth_attacks`). Each has a **unit layer that always runs** (no DB/secrets) and an **integration layer that skips cleanly** without a local Supabase. The full module list is in `docs/support-widget-internals.md`.
- Cross-workspace zero-leak: `python -m backend.test_us066_conversations_rls` (needs local Supabase + `DATABASE_URL`). API-edge auth attacks: `python -m backend.test_au4_auth_attacks`.
- Frontend gate is `npm run typecheck && npm run build` (multi-page; `tsc` is the lint gate - no ESLint config).
- Manual browser QA for the deferred UI checks: `docs/manual-test-plan-support-widget.md`.

## Where the full detail lives

- `docs/support-widget-internals.md` - the per-story sharp edges (US-066-093), rationale, and test descriptions relocated from here.
- `docs/widget-embed.md` - the loader `<script>` / cross-origin iframe embed contract.
- `docs/evals.md` + `docs/golden-set-authoring.md` - the eval harness, gate classes, and golden-set authoring.
- `docs/adr/` - committed ADRs (0001 RAGAS, 0002 tenant isolation, 0007 ingestion boundary). ADR-0003/0004/0008 are cited but not committed; their record is the internals doc + PRs #51-70.
- `CONTEXT.md` - domain language; `.claude/agent/tasks/prd-phase2-implementation.md` - the Phase-2 PRD + per-story status.
