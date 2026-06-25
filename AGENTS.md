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

## Support-bot retrieval: minted-JWT principal, and a NON-security workspace filter (US-070, ADR-0008)

The support bot is **not** a privileged retriever. Each customer turn,
`backend/support_bot.py:run_bot_deflection_turn` mints a ~60s `role=authenticated`
JWT with `sub = bot_user_id` (US-068 `mint_supabase_jwt`, dependency-injected to avoid a
`main.py` import cycle), runs the ADR-0003 deflection pipeline with that JWT in the Supabase
headers, then discards the token (no cross-turn cache). So `match_chunks`/`keyword_search` resolve
`auth.uid()` to the bot and the **existing** membership + owner-OR-ACL boundary applies wholesale —
the bot sees only documents shared to it via `chunk_acl` (share-to-bot). There is **no new content
role and no new retrieval predicate** (ADR-0008: a new issuer beside GoTrue, not a new enforcement
path). The minted token is a bearer credential: it must never reach an SSE event, response body, or
log line — only `DeflectionResult.customer_message` is client-safe.

Sharp edge — **`filter_workspace_id` is NOT the trust boundary.** Migrations
`20260624150000` / `20260624150100` add `filter_workspace_id uuid default null` to
`match_chunks` / `keyword_search` as an *ordinary non-security narrowing filter*, AND-ed beside
`filter_topics` (CONTEXT "Active workspace"; reserved by the US-007 note). It can only **subtract**
within what the `auth.uid()`-resolved membership clause already allows; `null` is a no-op, so
`/api/chat` (which passes no `workspace_id`) and E4/E6 are byte-identical. The boundary is the
membership EXISTS clause, never this param. Both hybrid legs carry it so the narrowing is coherent.
Pinned live by `backend/test_us070_bot_retrieval_integration.py` (bot sees only the shared doc; the
filter narrows without leaking) and in isolation by `backend/test_us070_bot_retrieval.py` (mint
per-turn, bot bearer + filter on both legs, token never in the result).

## Opaque per-conversation customer token (US-071, ADR-0008 amends ADR-0004)

The anonymous customer's reconnect credential is an **opaque random token, NOT a Supabase JWT** - do not confuse it with US-068's `mint_supabase_jwt` (that mints the *bot's* identity token with `SUPABASE_JWT_SECRET`; this token is signed with nothing).
`backend/conversation_tokens.py` holds the pure primitives: `generate_conversation_token()` returns 256-bit `secrets.token_urlsafe` bytes; `hash_conversation_token(raw)` returns the SHA-256 hex that is the **only** representation stored.
The raw token is returned in the clear **exactly once** at conversation creation (only to the iframe, via US-078's first-message flow) and must NEVER hit a log line, SSE event, or any other response body.
Continuity comes solely from the iframe-origin-stored raw token - there is **NO** server-side customer-identity table.

This is a **THIRD trust boundary** beside the two above, and it is narrower: backend-mediated only.
`20260624160000_conversation_tokens.sql` adds `conversation_tokens(token_hash pk, conversation_id fk, expires_at, created_at)` with **RLS enabled and ZERO policies** (anon/authenticated denied wholesale), plus a `resume_conversation(p_token_hash text, p_slide boolean default true)` SECURITY DEFINER RPC granted to `service_role` **only**.
The customer presents the raw token to the backend over HTTP (the `X-Conversation-Token` header, deliberately not `Authorization`); the backend hashes it and calls the RPC as the service role.
The RPC is the single authoritative read path: it resolves the hash to its bound conversation iff `expires_at > now() AND status <> 'resolved'`, and returns the **one** conversation - no caller-supplied id reaches it, so a token for X structurally cannot read Y.
`p_slide` gates only the activity refresh: `true` (default) slides the 24h window (the POST /resume path); `false` resolves+gates identically but skips the `expires_at` UPDATE, so the read-only GET transcript path never mutates token lifetime.
`workspace_membership.role` appears nowhere here (ADR-0002 intact).

- **Supabase default-privileges gotcha:** `revoke execute ... from public` is NOT enough - Supabase grants EXECUTE on new `public` functions to `anon`/`authenticated` **directly** (not via PUBLIC), so the migration revokes from `public, anon, authenticated` by name, then grants to `service_role`. Verify with `has_function_privilege('anon', 'public.resume_conversation(text, boolean)', 'EXECUTE')` = false.
- **Invalidated on resolve, twice over:** the RPC's `status <> 'resolved'` gate plus an AFTER-UPDATE trigger (`conversations_purge_tokens_on_resolve`) that **deletes** the conversation's token rows when it transitions into `resolved` (literal invalidation + bounded growth). The BEFORE-UPDATE US-067 status guard has already validated the transition by the time it fires.
- **Lifetime ~24h**, slid forward on every successful *POST /resume* while not resolved; refresh extends `expires_at` on the *same* token (the raw token is stable across reloads - it is "returned once", never rotated). A GET transcript read is **not** activity and never slides the window.
- Endpoints (`main.py`): `POST /widget/conversations/resume` and `GET /widget/conversations/{id}/transcript`, both authed by the opaque token via `_resume_conversation_by_token`. POST resumes with `slide=True` (activity refresh); the **GET transcript path passes `slide=False`** so a nominally-safe/idempotent GET (browser prefetch, link-preview crawler, transparent retry) can never extend the token's lifetime as a side effect of the binding check. The transcript endpoint enforces the binding (resumed conversation id must equal the path id) so "token-for-X requesting Y" is rejected, and constrains the message read to customer-visible roles (`role=in.(user,assistant)`). Both helpers fail closed via `_require_service_role_headers()` (503 when `SUPABASE_SERVICE_ROLE_KEY` is unset). `_issue_conversation_token(http, conversation_id)` is the issuance helper US-078 will call on first message. Public-widget CORS is US-074's concern; these routes do not widen the `/api/*` `FRONTEND_ORIGINS` posture.

Test: `python -m backend.test_us071_conversation_tokens` - a unit layer (always runs: token is opaque/unique/URL-safe/not-JWT-shaped, hash is one-way 64-hex `!=` raw, 24h TTL) plus an integration/security layer (skips cleanly without local Supabase) encoding the PRD validation test: resume(Tx) returns X only (resume(Ty)->Y positive control), Tx can never resolve to Y, anon cannot call the RPC (service-role can), expiry/refresh behave, and resolving X purges Tx and rejects its resume.

## Widget keys: the non-secret public key + the not-revoked resolution gate (US-072, ADR-0008)

`widget_keys` (`supabase/migrations/20260624130000_widget_keys.sql`) is the public widget's key registry.
`public_key` is **NON-SECRET** - it is embedded verbatim in the buyer's page JS (the loader `<script>`, US-083), stored in the clear (nothing to hash, unlike US-071's `conversation_tokens.token_hash`), and grants NO access by itself: it only NAMES which workspace's bot a widget speaks to.
`backend/widget_keys.py` holds the pure primitives (`generate_public_key()` -> `wk_pk_<urlsafe>`; `is_widget_public_key()` shape guard); the leaked-key blast radius is the already-public KB, and the hard abuse controls are the rate limit + circuit breaker (US-076/077), NOT the key's secrecy.

This is a **FOURTH** boundary beside the three in this file, and it is the **admin-management** surface:

- **`role='admin'` legitimately enters the RLS here - the ONE place it may.** ADR-0002 bars `role` from *retrieval/visibility* predicates, but issuing/rotating/revoking keys IS the administrative action the admin role exists for. So the `widget_keys` SELECT/INSERT/UPDATE policies gate on the membership clause WITH `and wm.role = 'admin'`. There is intentionally **NO DELETE policy** (rotate = issue-new + revoke-old; a revoked key is retained for audit, deny-by-default delete).
- **Two faces on different CORS/auth surfaces.** Admin management (`/api/support/widget-keys*`, US-090's caller) runs INSERT/SELECT/UPDATE under the admin's *own* JWT so the admin RLS IS the authorization (a non-admin's write is rejected by Postgres, not app code). Public resolution (`POST /widget/keys/resolve`) is anonymous and reads under the **service role** (the widget holds no Postgres role), gating on `revoked_at IS NULL` and leaking no workspace topology. Public-widget CORS is US-074, per-key origin enforcement US-073, rate-limiting US-076 - all layer on top without changing this gate; these routes do not widen the `/api/*` `FRONTEND_ORIGINS` posture.
- **Resolution is the not-revoked gate.** `_resolve_widget_key` is a service-role read filtered by `public_key=eq AND revoked_at=is.null`; a revoked/unknown key matches **zero rows** and mints nothing. Revoking blocks NEW conversations but **never terminates a live one** - the opaque per-conversation token (US-071) is independent of the key once minted.
- **Revocation is a one-way DB-enforced latch**, the same "enforced in the DB, not just the service layer" stance as the US-067 status machine. A `before update` trigger (`public._widget_keys_revoke_guard` / `widget_keys_revoke_guard`) rejects any mutation of an already-set `revoked_at` (cannot clear to NULL, cannot move the timestamp), so a revoked key can never be re-activated by a direct PostgREST PATCH behind the admin UPDATE policy (which is column-unrestricted on purpose). `NULL -> timestamp` (the revoke action) and `NULL -> NULL` (label/origin edits) stay allowed; admins may still freely edit `label`/`allowed_origins` even on a revoked row.
- **First-key-issuance bot provisioning.** `_ensure_workspace_bot` (main.py) is the single, idempotent, best-effort call site for US-069's `support_bot.provision_workspace_bot(workspace_id, http=...)` (`workspace_id` is the only positional; `http`/url/key are keyword-only). First issuance provisions the per-workspace bot; later issuances return the same id. Provisioning is best-effort - a failure (or a build without the support-bot module) is logged and never blocks key issuance, and US-069 is also triggered lazily at first conversation (US-078).

Test: `python -m backend.test_us072_widget_keys` - a unit layer (always runs: public key prefixed/unique/URL-safe, shape guard rejects blank/prefix/garbage) plus an integration/security layer (skips cleanly without local Supabase) encoding the PRD validation test: active K resolves to its workspace while revoked Kr resolves to 0 rows (no minting), an admin reads both keys + can insert/revoke while a non-admin member and a non-member read 0 and cannot write, and revoking Kr leaves the live conversation + its US-071 token fully resumable.

### Per-key registered-origin allowlist, fail-closed (US-073, ADR-0008)

`widget_keys.allowed_origins text[]` (already added by US-072's migration - **no new migration**) carries a per-key registered-origin allowlist enforced at public resolution.
`backend/widget_keys.py:is_origin_allowed(origin, allowed_origins)` is the pure, always-unit-testable matcher; `main.py:widget_resolve_key` reads the request `Origin` header (added a `request: Request` param) and refuses with the SAME opaque 404 (`"unknown or inactive widget key"`) when it returns False, so nothing leaks whether the key exists or which origins it allows. The not-revoked gate (`_resolve_widget_key`) stays FIRST; origin is an additional gate layered on top, ordered after, so a revoked key and an unlisted origin are indistinguishable in the response.

- **Fail-closed by construction, never fail-open** - every ambiguous case refuses: an empty `[]` OR null allowlist makes the key **INACTIVE** (a key with no registered origin resolves nothing); a missing OR blank `Origin` is refused (the cross-origin widget always sends one - absence means refuse, even under the wildcard). `[]`/null (empty, inactive) is kept distinct from `["*"]` (non-empty, active wildcard).
- **`"*"` is a documented DEV-ONLY opt-in (PRD F3 row), non-production**: a key whose `allowed_origins` contains `"*"` matches any *present* origin (`WILDCARD_ORIGIN` in `widget_keys.py`). It still refuses an originless request.
- **Comparison is exact, un-normalized string membership** - origins are scheme+host[+port], no path/trailing slash, exactly the form the browser emits in `Origin` (which it already lower-cases). Any mismatch (casing, stray slash, port) fails CLOSED - the safe direction - so admins must register origins in that canonical form.
- **Defense-in-depth, NOT a hard control.** The `public_key` is non-secret and `Origin` is trivially forgeable off-browser, so this only blunts casual key-lifting and in-browser cross-site abuse. The hard abuse controls remain the rate limit + circuit breaker (US-076/077); the leaked-key blast radius is the already-public KB. Do not overstate it as a security boundary. Scope is the `/widget/keys/resolve` path; US-078 (lazy conversation creation) re-resolves server-side and **reuses the same `is_origin_allowed` helper**.

Test: `python -m backend.test_us073_widget_key_origin` - a unit layer (always runs, security-critical: listed allowed / unlisted refused / empty `[]` + null allowlist refused / missing+blank+whitespace origin refused / `"*"` allows any present origin alone or mixed / `"*"` still refuses an originless request / exact-match so trailing-slash+case+port mismatches fail closed) plus an integration/security layer driving the real `POST /widget/keys/resolve` endpoint through a FastAPI TestClient with the US-072 resolve gate mocked (the origin gate lives in the endpoint, not the DB): key A (`['https://client.example']`) + matching `Origin` -> 200, A + unlisted origin -> 404, B (empty allowlist) -> 404 inactive, originless A -> 404, every refusal the same opaque body.
