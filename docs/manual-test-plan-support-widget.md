# Manual test plan - support widget (US-086 to US-090, US-112)

This is the step-by-step manual QA plan for the browser-based acceptance criteria that were deferred during implementation.
Every one of these stories has its data-layer / API contract already proven live (PostgREST assertions, backend `test_us0XX_*.py` suites); what remains is the human click-through the CI environment could not run.
This document is that click-through.

Scope:

- **US-086** - share-to-bot is a separate, explicitly-confirmed publish action (bot never grantable from the normal share box).
- **US-087** - `/support/queue` membership-gated escalated list, live via Realtime (already verified in browser 2026-07-01; included here as a prerequisite and re-check).
- **US-088** - queue conversation view: read transcript, reply, Resolve.
- **US-089** - optional unenforced soft-claim (claiming dims a row for other agents; a non-claimer can still reply).
- **US-090** - `/support/settings` admin-gated (issue/rotate/revoke keys; non-admin blocked).
- **US-112** - demo-corpora worked-examples doc review (NOTE: the doc is not authored yet - this section is the review checklist for when it lands).

Each test ends with an explicit PASS / FAIL line keyed to the story's Failure Indicator.
Record results in the summary table at the bottom.

---

## 0. One-time environment setup

You stand this up once, then run every US-086 to US-090 test against it.
Budget ~30 minutes for first-time setup.

### 0.1 Prerequisites

- Node 20+, Python 3.11+, Docker Desktop running, Supabase CLI **pinned to 2.90.0** (a newer CLI's key format 403s self-minted JWTs - see the retrieval-eval-CI memory), and an `OPENAI_API_KEY`.

### 0.2 Start Supabase (terminal 1)

```bash
cd /Users/hcho/Developer/Agentic_RAG
supabase start
supabase status     # copy API URL, anon key, service_role key, DB URL
```

Note the values from `supabase status`:

- API URL: `http://127.0.0.1:54321`
- DB (direct SQL / psql): `postgresql://postgres:postgres@localhost:54322/postgres`
- Studio (SQL editor in a browser): `http://localhost:54323`
- anon key and service_role key: copy the two JWTs it prints (they are not committed to the repo - always read them live).

All 43 migrations in `supabase/migrations/` apply automatically on `supabase start`.
If you need a clean slate later: `supabase db reset`.

### 0.3 Configure and start the backend (terminal 2)

```bash
cd /Users/hcho/Developer/Agentic_RAG/backend
cp .env.example .env
```

Edit `backend/.env` and set at minimum:

```
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_ANON_KEY=<anon key from supabase status>
SUPABASE_SERVICE_ROLE_KEY=<service_role key from supabase status>
SUPABASE_JWT_SECRET=<the JWT secret from supabase status / config>   # REQUIRED for the support bot
OPENAI_API_KEY=sk-...
FRONTEND_ORIGIN=http://localhost:5173
```

Gotchas confirmed from the code:

- The env var is `FRONTEND_ORIGIN` (singular). There is no `FRONTEND_ORIGINS`.
- `SUPABASE_SERVICE_ROLE_KEY` gates the ENTIRE `/widget/*` surface. If it is unset, every widget endpoint returns 503 and nothing below will work.
- `SUPABASE_JWT_SECRET` is required specifically for the support bot to mint its ~60s retrieval token. Without it the bot cannot answer and every customer turn degrades to the generic deferral (which is fine for the queue tests, but not for seeing a real bot answer).

Then:

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Backend is now on `http://localhost:8000`.

### 0.4 Configure and start the frontend (terminal 3)

```bash
cd /Users/hcho/Developer/Agentic_RAG/frontend
cp .env.example .env
```

Edit `frontend/.env`:

```
VITE_SUPABASE_URL=http://127.0.0.1:54321
VITE_SUPABASE_ANON_KEY=<anon key from supabase status>
VITE_BACKEND_URL=http://localhost:8000
```

Then:

```bash
npm install
npm run dev          # http://localhost:5173  (kit origin; also serves widget.js + widget.html)
```

### 0.5 Serve the widget host fixture on a DIFFERENT origin (terminal 4)

The cross-origin boundary is the whole security model, so the host page must be a different origin from the kit.

```bash
cd /Users/hcho/Developer/Agentic_RAG/frontend
python3 -m http.server 8099
# host page: http://localhost:8099/widget-host-example.html
```

### 0.6 Create the three test users

Signup is enabled and email confirmation is OFF, so a signed-up user is immediately usable.
Seeder-created `auth.users` rows CANNOT log in interactively (they are inserted with an empty password for RLS/attribution only), so create the login users through the app UI.

In the browser at `http://localhost:5173`, sign up three users (use any password):

| Handle | Email (suggestion) | Role you will assign | Used by |
|--------|--------------------|-----------------------|---------|
| **Ad** | `admin@test.local` | `admin` | US-090 admin, US-086 doc owner |
| **A1** | `agent1@test.local` | `member` | US-088 / US-089 agent |
| **A2** | `agent2@test.local` | `member` | US-089 second agent, US-088 cross-workspace negative |

After signing each up, log out so the next signup starts clean.
Then grab each user's UUID from Studio (`http://localhost:54323`, SQL editor):

```sql
select id, email from auth.users order by created_at;
```

### 0.7 Create a workspace and assign roles

Pick one workspace W for the "happy path" and a second workspace W2 for the cross-tenant negative test.
In Studio, run (substitute the real UUIDs):

```sql
-- Workspace W (the shared support workspace)
insert into public.workspaces (id, name, owner_id)
values ('11111111-1111-1111-1111-111111111111', 'Acme W', '<Ad_uuid>');

-- Workspace W2 (a different tenant, to prove zero cross-workspace leak)
insert into public.workspaces (id, name, owner_id)
values ('22222222-2222-2222-2222-222222222222', 'Other W2', '<A2_uuid>');

-- Memberships in W
insert into public.workspace_membership (workspace_id, user_id, role) values
  ('11111111-1111-1111-1111-111111111111', '<Ad_uuid>', 'admin'),
  ('11111111-1111-1111-1111-111111111111', '<A1_uuid>', 'member');

-- A2 is a member of W2 ONLY (the cross-tenant party)
insert into public.workspace_membership (workspace_id, user_id, role) values
  ('22222222-2222-2222-2222-222222222222', '<A2_uuid>', 'admin');
```

Note: if `workspaces` has a different column shape in your migrations, adjust column names - inspect with `\d public.workspaces` / `\d public.workspace_membership`.
The `role` values that matter are exactly `admin` and `member`.
Keep each login user a member of a SINGLE workspace so the client-side `resolveActiveWorkspace` picks a default rather than showing the ambiguous note (US-087). If you make a user a member of two workspaces you will see the "ambiguous" note instead of a queue, which is correct behavior but not what these tests exercise.

### 0.8 Provision the bot + issue a widget key (this is also US-090 AC coverage)

Bot provisioning is lazy: it happens automatically on the FIRST widget-key issuance.
Do this through the admin UI (which is itself US-090):

1. Log in as **Ad** at `http://localhost:5173`.
2. Confirm the header shows a **Support settings** link (admin-only). Navigate to `/support/settings`.
3. Issue a key: label `local-test`, allowed origins = `http://localhost:8099` (the host fixture origin). Submit.
4. Copy the resulting `wk_pk_...` public key.

This one action (a) provisions the workspace bot (US-069) and (b) gives you the public key the host fixture needs.

CLI fallback (if you want to script it) - issue the key with the admin's JWT:

```bash
# Get Ad's access token from the browser devtools (Application > Local Storage > supabase auth token)
# or mint one; then:
curl -sS -X POST http://localhost:8000/api/support/widget-keys \
  -H "Authorization: Bearer <Ad_access_token>" \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"11111111-1111-1111-1111-111111111111","label":"local-test","allowed_origins":["http://localhost:8099"]}'
```

Verify the bot now exists:

```sql
select wm.user_id, wm.role, wm.is_bot
from public.workspace_membership wm
where wm.workspace_id = '11111111-1111-1111-1111-111111111111' and wm.is_bot;
-- expect exactly ONE row, role='member', is_bot=true
```

### 0.9 Seed a ready document owned by Ad (for US-086)

US-086 needs a document in `status='ready'` owned by the doc owner.
Easiest path: log in as **Ad**, go to the Ingestion / upload surface in the app, upload a small text/markdown file, and wait until it shows **ready**.
Confirm:

```sql
select id, title, status, user_id from public.documents where user_id = '<Ad_uuid>';
-- expect at least one row with status='ready'
```

Point the host fixture at your key: open `http://localhost:8099/widget-host-example.html`, and if it does not already read the key from a field, edit `frontend/widget-host-example.html` so the loader `<script>`'s `data-public-key` is your `wk_pk_...` (and `data-position`, `data-brand-color`, etc. as you like).

Setup is complete. You now have: Supabase + backend + frontend + a cross-origin host page, three users (Ad=admin, A1/A2=member), workspaces W and W2, a provisioned bot, a widget key scoped to `:8099`, and a ready document.

---

## US-086 - Share-to-bot is a separate, explicitly-confirmed publish action

**Goal:** prove the bot can NEVER be granted from the normal teammate share box, and that publishing to the widget is a distinct, explicitly-confirmed action.
**Failure indicator:** typing the bot's email in the normal box silently shares the doc to the public widget.

### Steps

1. Log in as **Ad**. Open the ready document from 0.9 and open its **Share** dialog.
2. Find the bot's email:
   ```sql
   select u.email
   from auth.users u
   join public.workspace_membership wm on wm.user_id = u.id
   where wm.workspace_id = '11111111-1111-1111-1111-111111111111' and wm.is_bot;
   -- e.g. support-bot-...@bots.support.internal
   ```
3. In the normal "share with a teammate" grant box, type that bot email and try to grant.
   - **Expected:** a clear rejection (HTTP 403 surfaced as an error), NOT a silent success. The bot must not appear as a grantee row.
   - Confirm zero grant was written:
     ```sql
     -- capture the doc's chunk ids, then check chunk_acl for the bot principal is unchanged/zero
     select count(*) from public.chunk_acl ca
     join public.chunks c on c.id = ca.chunk_id
     where c.document_id = '<doc_id>' and ca.principal_id = '<bot_uuid>';
     -- expect 0
     ```
4. Positive control: in the same box, grant to a real teammate (e.g. A1's email). It should succeed and appear as a grantee row.
5. Now use the dedicated **Public support widget** section of the Share dialog. Click **Publish...**.
   - **Expected:** a nested confirmation dialog appears stating the consequence verbatim: contents become "answerable to anyone who can reach your public support widget". Publishing must require this explicit confirmation.
6. Confirm the publish. Verify the bot grant now exists:
   ```sql
   select count(*) from public.chunk_acl ca
   join public.chunks c on c.id = ca.chunk_id
   where c.document_id = '<doc_id>' and ca.principal_id = '<bot_uuid>';
   -- expect = the doc's chunk count (one acl row per chunk)
   ```
7. Reopen `GET /shares` (the teammate list): the bot must NOT appear as a grantee row there - only the real teammate from step 4. The published state is reported only by the dedicated publish section.
8. Unpublish (the section now shows an amber banner + Unpublish) and confirm the bot grants are revoked:
   ```sql
   -- expect 0 again
   ```

### Result

- PASS if: step 3 is blocked (403, zero acl rows), step 4 succeeds, step 5 shows the explicit consequence confirmation, step 6 writes the acl rows only after confirming, step 7 hides the bot from the teammate list.
- FAIL if: typing the bot email in the normal box silently grants (rows written) or the bot shows as an ordinary grantee row.

---

## US-087 - `/support/queue` membership-gated live list (prerequisite / re-check)

Already verified in browser 2026-07-01; re-run it here because US-088/089 build on it.
**Failure indicator:** the queue gates on `role`, requires a manual refresh, or shows cross-workspace conversations.

First, create an escalated conversation in W. Two ways:

**Option A - pure SQL (reliable, no OpenAI needed):**

```sql
-- a conversation in W, born active, with the provisioned bot
insert into public.conversations (id, workspace_id, bot_user_id, status, channel)
values ('aaaaaaaa-0000-0000-0000-000000000001',
        '11111111-1111-1111-1111-111111111111',
        '<bot_uuid>', 'active', 'widget');

-- a couple of transcript turns
insert into public.conversation_messages (conversation_id, role, content) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'user', 'How do I return an item?'),
  ('aaaaaaaa-0000-0000-0000-000000000001', 'assistant', 'You can return within 30 days...');

-- escalate it (set ONLY status; the US-067 trigger stamps escalated_at itself)
update public.conversations set status='escalated'
where id = 'aaaaaaaa-0000-0000-0000-000000000001';
```

**Option B - full widget flow (end-to-end, needs OpenAI + bot):** open the host page at `:8099`, send a customer message, then click the "Talk to a human" button (US-091). This escalates via the real endpoint.

### Steps

1. Log in as **A1** (a plain `member`). Navigate to `/support/queue`.
   - **Expected:** the escalated conversation appears (A1 is only a member - proves membership-gated, not role-gated).
2. With A1's queue open, escalate a SECOND conversation live (repeat Option A with a new id, or use Option B in another tab).
   - **Expected:** the new row appears with NO page refresh (Realtime). Rows are ordered oldest-escalation-first.
3. Resolve one conversation live: `update public.conversations set status='resolved' where id='...';`
   - **Expected:** it disappears from A1's queue with no refresh.
4. Log in as **A2** (member of W2 only, NOT W). Navigate to `/support/queue`.
   - **Expected:** A2 sees ZERO of W's conversations.

### Result

- PASS if: A1 (member) sees W's escalated rows, a new escalation appears live, a resolve removes it live, and A2 sees none of W's rows.
- FAIL if: role-gated, needs manual refresh, or cross-workspace rows leak.

---

## US-088 - Queue conversation view: read transcript, reply, Resolve

**Goal:** an agent opens an escalated conversation, reads the transcript, posts a reply that reaches the customer, and Resolves it (which invalidates the customer token).
**Failure indicator:** reply does not reach the widget, Resolve does not invalidate the token, or a resolved conversation stays in the queue.

Use an escalated conversation from US-087 (Option A gives you a transcript to read).
For the "reply reaches the widget live" leg you want a real customer widget open, so prefer Option B here: open `:8099`, send a message, click "Talk to a human". Keep that customer tab open.

### Steps

1. Log in as **A1**, go to `/support/queue`, click the escalated conversation to open the detail pane.
   - **Expected:** the full transcript renders (user + assistant turns), oldest first. Only `user`/`assistant` turns show; no `tool_calls` tree is rendered.
2. In the composer, type an agent reply and send.
   - **Expected (backend):** the reply is written as a `role='assistant'` row.
   - **Expected (customer):** in the open customer widget tab, the reply appears live (fanned over the customer SSE, or via the interim transcript poll within a few seconds).
   - Confirm the row:
     ```sql
     select role, content from public.conversation_messages
     where conversation_id='<conv_id>' order by created_at;
     -- your reply is the last row, role='assistant'
     ```
3. Cross-workspace negative: log in as **A2** and attempt to reach/reply to W's conversation. A2 must not be able to open it or reply (membership RLS -> 404/403). (You can also hit the endpoint directly with A2's token to confirm a 404.)
4. Back as **A1**, click **Resolve**. Confirm the explicit confirmation dialog appears (Resolve is final and closes the customer session). Confirm it.
   - **Expected:** the conversation flips to `resolved` and leaves the queue live.
   - **Expected:** the customer's reconnect token is purged and a resume is rejected:
     ```sql
     select status, escalated_at from public.conversations where id='<conv_id>';
     -- status='resolved', escalated_at PRESERVED (not null, not reset)
     select count(*) from public.conversation_tokens where conversation_id='<conv_id>';
     -- expect 0 (purged on resolve)
     ```
   - In the customer widget, the session should close on its next revalidation (attempting to send should start-fresh / fail to resume).

### Result

- PASS if: transcript renders (no tool-call tree), the agent reply reaches the customer widget, a cross-workspace agent is rejected, Resolve sets `resolved` + preserves `escalated_at` + purges the token + removes the row from the queue.
- FAIL if: reply never reaches the widget, Resolve leaves the token valid, or a resolved conversation stays listed.

---

## US-089 - Optional unenforced soft-claim

**Goal:** claiming a conversation dims the row for OTHER agents, but is purely advisory - a non-claimer can still reply.
**Failure indicator:** the claim blocks another agent's reply (that would make it an enforced assignment - out of scope).

You need two agents who are both members of W with the queue open.
A1 already is; make A2 a member of W too for this test (temporarily), so both see the same queue:

```sql
insert into public.workspace_membership (workspace_id, user_id, role)
values ('11111111-1111-1111-1111-111111111111', '<A2_uuid>', 'member')
on conflict do nothing;
```

Note: adding A2 to W makes A2 a member of TWO workspaces, which triggers the US-087 "ambiguous active workspace" note for A2.
For this test, either remove A2 from W2 first, or accept that you may need to drive A2 via a direct claim/reply if the ambiguous note blocks the queue UI.
Cleanest: temporarily remove A2 from W2 so A2's sole workspace is W.

Create a fresh escalated conversation C in W (Option A from US-087).

### Steps

1. Open two browser sessions (two profiles or one normal + one incognito): **A1** and **A2**, both at `/support/queue`, both seeing C.
2. As **A1**, open C and click **Claim**.
   - **Expected (A1):** a claim pill shows "Claimed by you"; A1's own row is NOT dimmed.
   - **Expected (A2):** within a moment (live over the existing Realtime feed, no refresh), C's row DIMS (opacity reduced) and shows "Claimed by <A1 email or 'another agent'>".
   - Confirm:
     ```sql
     select claimed_by, claimed_at from public.conversations where id='<C_id>';
     -- claimed_by = A1_uuid, claimed_at set
     ```
3. As **A2**, open C (still openable despite the dim - dimming is not a gate) and send a reply.
   - **Expected:** A2's reply SUCCEEDS (HTTP 201 / appears in the transcript). This is the key assertion: the claim never blocks a reply.
4. As **A2**, click **Claim anyway** (take over).
   - **Expected:** `claimed_by` flips to A2 (last-write-wins); now A1's view dims C.
5. As the current claimer, click **Release**.
   - **Expected:** `claimed_by`/`claimed_at` clear to null; the dim disappears for everyone.
6. Non-member negative: confirm a non-member's claim writes nothing (membership RLS). A2-when-not-a-member-of-W, or any outside user, claiming C affects 0 rows.

### Result

- PASS if: claiming dims the row only for OTHER agents, shows the claimer identity, updates live, take-over is last-write-wins, release clears it, AND A2 can still reply to a claimed C.
- FAIL if: the claim blocks A2's reply, or dimming the row also disables its reply/resolve.

---

## US-090 - `/support/settings` admin-gated

**Goal:** an admin can issue/rotate/revoke widget keys; a non-admin member is blocked from the settings route but still reaches the queue.
**Failure indicator:** a non-admin reaches settings, or rotation does not revoke the old key.

### Steps

1. Log in as **Ad** (admin of W). Confirm the header shows the **Support settings** link and `/support/settings` loads the admin surface.
2. Issue a key with label `rotate-test` and one origin `http://localhost:8099`. Submit.
   - **Expected:** the key appears as an active `wk_pk_...` card with a copyable public key and embed snippet.
   - Empty-allowlist guard: try to submit with NO origins. The submit is disabled / rejected (fail-closed, matching the backend 400). A `*` entry is accepted but flagged dev-only.
3. **Rotate** the key (issue-new + revoke-old).
   - **Expected:** a NEW active key appears; the OLD key moves to a "revoked / kept for audit" state.
   - Confirm at the DB:
     ```sql
     select public_key, revoked_at from public.widget_keys
     where workspace_id='11111111-1111-1111-1111-111111111111' order by created_at;
     -- old key has revoked_at set; new key has revoked_at null
     ```
4. Revoke a key explicitly (behind its confirmation). Confirm `revoked_at` is set and cannot be cleared (the DB latch rejects un-revoking).
5. Non-admin gate: log in as **A1** (`member`). Navigate to `/support/settings`.
   - **Expected:** A1 sees an "admins only" note (the nav link is also hidden for non-admins). A1 must NOT be able to issue/read keys.
   - Confirm the hard boundary: even if A1 hits `POST /api/support/widget-keys` directly with A1's token, Postgres RLS rejects it (403) and a list read returns `[]`.
6. Confirm A1 CAN still reach `/support/queue` (membership-gated, unaffected by the admin gate).

### Result

- PASS if: Ad can issue/rotate/revoke (old key shows revoked, new active), the empty-allowlist guard fires, A1 is blocked from settings (UI note + server 403) but still reaches the queue.
- FAIL if: a non-admin reaches settings and issues a key, or rotation leaves the old key active.

---

## US-112 - Demo-corpora worked-examples doc (review checklist)

**IMPORTANT:** this story is NOT yet implemented - the doc does not exist yet.
This section is the acceptance review to run ONCE the doc is authored (it is docs-only; there is nothing to click through in a browser).
Until the doc is written, US-112 is FAIL by default (nothing to review).

When the doc exists, confirm each acceptance criterion:

1. **Three role-specific examples present and correctly framed:**
   - e-commerce - the DEFAULT (permissions + escalation, small/fast/relatable).
   - Wikipedia 10k - a scale-benchmark, marked **filler only / never golden-answerable** (golden questions stay anchored to the real docs).
   - CRM - the text-to-SQL optional-module example (X1).
2. **Honest swap framing present:** the doc states plainly that swapping in the buyer's own corpus makes the example golden set's anchors **fail loud** (a direct consequence of content anchoring, US-107); the example set is a **format template to learn from, not a survives-the-swap artifact**. The doc must NOT imply the example questions will work on the buyer's docs.
3. **Cross-references present:** links to US-110 (seeder = corpus only) and US-108 / US-109 (author a new golden set on swap).
4. **Typecheck/lint:** docs-only, so this is vacuously green (no markdown lint harness in the repo); just confirm internal cross-references resolve to real section headers.

### Result

- PASS if: all three corpora are labeled with their role, Wikipedia is marked filler-only/never-gold, and the swap-fail-loud + "replace corpus = author new golden set" framing is present.
- FAIL if: the doc is missing, presents the corpora as interchangeable defaults, or implies the example golden set survives a buyer corpus swap.

---

## Summary - record results

| Story | What it proves | Result | Notes |
|-------|----------------|--------|-------|
| US-086 | bot never grantable from normal share box; publish is explicit + confirmed | [x] PASS / [ ] FAIL | verified 2026-07-10 |
| US-087 | queue is membership-gated + live; no cross-workspace leak | [x] PASS / [ ] FAIL | already verified 2026-07-01 |
| US-088 | transcript renders; agent reply reaches widget; Resolve invalidates token | [ ] PASS / [ ] FAIL | |
| US-089 | claim dims for others but never blocks a reply | [ ] PASS / [ ] FAIL | |
| US-090 | admin issues/rotates/revokes; non-admin blocked from settings, not queue | [ ] PASS / [ ] FAIL | |
| US-112 | demo-corpora doc framing + swap honesty | [ ] PASS / [ ] FAIL | doc not authored yet -> FAIL until written |

### Cleanup

```bash
supabase stop        # or `supabase db reset` to wipe and re-apply migrations
```

Kill the four terminals (Supabase, uvicorn, vite, http.server).
