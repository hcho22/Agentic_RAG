# PRD — Phase 2 Build: Permission-Aware RAG + AI-Support Starter Kit

**Status:** Implementation-ready. Decomposes on-disk ADRs **0002–0008** (and the `CONTEXT.md` designs they reference) into buildable, independently-shippable user stories. Companion to `.claude/agent/tasks/PRD_phase2_agentic_rag_kit.md` (the strategy/scoping PRD); governed by `CONTEXT.md` (terminology) and `docs/adr/` (decisions). Generated 2026-06-17.

**Scope:** the full remaining Phase-2 build — workspace tenant isolation, config-driven model surface, ingestion parser boundary, escalation/deflection pipeline, the anonymous support-widget surface + conversation/handoff, and the eval-gate + golden-set genericization. **Self-contained:** prerequisite stories (e.g. the workspace layer the support surface sits on) are *included*, not assumed.

**Designed but OUT of this PRD** (CONTEXT-only — no ADR in the 0002–0008 set): Observability **O1–O4** (trace facade/dashboard), **F2** typed TS client, **F3** capability matrix, the **P-series** productization. They are cross-referenced where sections owe them artifacts (especially F3 rows — see the tail) and belong in a follow-up PRD. **AU3** (identity-boundary doc) rides along in §A because it is ruled by ADR-0002.

---

## 1. Overview

The kit is one retrieval/eval engine with two faces — a knowledge-assistant ("chat with your docs") and a customer-facing support platform — sharing the same permission-aware retrieval, isolation, and eval machinery. Phase 2 adds the org-isolation half of the hero (workspaces), the support face (escalation + an embeddable widget + human handoff), the portability work (config-driven model + ingestion boundaries), and turns the project's evals into buyer-authorable methodology with a configurable CI gate.

The moat is **demonstrated, not asserted**: access-correctness and answer-trustworthiness are *proven by evals in CI* (zero gold-chunk leakage across viewers and across workspaces; deflection maximized under a buyer-set false-resolve ceiling). Every security-critical story below carries an assert-style validation test with an exact outcome (`0 rows` / `rejected` / `cannot read`), because a single leak inverts the product thesis.

## 2. Goals

- **Org isolation:** a Workspace as a hard tenant boundary enforced *inside* `match_chunks` from `auth.uid()`, proven by an additive zero-leak eval (E6) and API-layer auth attack tests (AU4).
- **Support face:** a first-class escalate-vs-resolve decision (deterministic, eval-scored), an embeddable widget that keeps an anonymous customer structurally off the trust surface, and a membership-gated human-handoff loop.
- **Portability:** one OpenAI-Chat-Completions provider contract with first-class OpenAI + Azure targets, and one swappable `DocumentParser` seam — both fail-closed where silence would be dangerous.
- **Eval as a product:** a two-class CI gate (un-downgradable security floor + tunable quality gate) and a layered, content-anchored golden set a buyer authors once.
- **Ships green day-zero:** `seed → eval` produces the 1.000 no-leak table on the first run.

## 3. How to read

- Stories are grouped by area (A–F), numbered `US-NNN` (ranges are non-contiguous; gaps are reserved). Each is small enough for one focused session.
- Acceptance criteria are concrete and verifiable; UI stories require browser verification; **security stories assert exact outcomes**.
- Every story has a **Validation Test** (setup / steps / expected / failure indicator) — a runnable mini-QA script.
- Per-area Functional Requirements and Non-Goals close each section; global cross-cutting concerns, shared constants, owed F3 rows, success metrics, and open questions are in the tail.

## 4. Build order & dependencies

Topological order (each section depends only on those above it):

| § | Area | ADR | US range | Depends on |
|---|---|---|---|---|
| A | Workspace tenant isolation | 0002 | US-001–011 | — (foundation) |
| B | Config-driven model surface | 0006 | US-021–028 | — (infra; provides the runtime judge role D needs) |
| C | Ingestion parser boundary | 0007 | US-036–045 | — (infra, independent) |
| D | Escalation & deflection pipeline | 0003 | US-046–059 | A (retrieval context), B (judge role) |
| E | Support surface (widget + handoff) | 0008 + 0004 | US-066–093 | A (membership/bot), D (per-turn decision) |
| F | Eval gate + golden-set genericization | 0005 | US-101–112 | A/D/E (classifies E4/E6/E7/AU4) |

**Reconciliation with the strategy PRD §9:** that rollout order (workspace → escalation → observability → eval → model/boundaries → support) is *go-to-market phasing*; this PRD orders by *hard build dependency*, which forces the model surface (B) ahead of escalation (D) because D's runtime faithfulness gate is B's judge role. Observability is excluded here (CONTEXT-only). Sections A–C are independently shippable in parallel; D requires A+B; E requires A+D; F lands last to classify the evals the others create.

## 4a. Implementation status (updated 2026-06-17)

Progress is tracked per-story via the `[x]`/`[ ]` acceptance-criteria checkboxes below and a `**Status:**` line on each touched story.

| Story | Status | Evidence |
|---|---|---|
| US-001 (workspace + membership schema) | ✅ **Done** | `20260617120000_init_workspaces.sql`, `20260617120100_init_workspace_membership.sql`; verified live: both tables, the `(user_id, workspace_id)` index, and `workspace_membership_select_own` RLS policy exist. |
| US-002 (Default Workspace backfill + `documents.workspace_id`) | ✅ **Done** | `20260617120200_default_workspace_backfill.sql` + seeder mirroring in `db_seed/corpus_seed.py` and `evals/retrieval/runner.py`; verified live: `documents.workspace_id` is NOT NULL (0 null rows), Default Workspace row present. |
| US-003 (membership clause inside `match_chunks`) | ✅ **Done** | `20260617120300_match_chunks_workspace_membership.sql`; verified live: clause present, function still `stable`/`security invoker`/correct `search_path`, `authenticated` GRANT intact. Transactional validation test passes (member+owner → 1 row; owner with membership revoked → 0 rows, proving AND-ed/subtractive). |
| US-004 (membership clause inside `keyword_search`, hybrid parity) | ✅ **Done** | `20260617120400_keyword_search_workspace_membership.sql`; verified live: clause present, attrs intact, GRANT intact. Transactional validation test passes (member+owner `keyword_search` → 1 row; revoked → 0 rows). `hybrid_search` inherits the boundary on both legs with no `retrieval.py` change. |
| US-005 (membership mirror in `chunks`/`documents` RLS, defense in depth) | ✅ **Done** | `20260617120500_chunks_documents_workspace_rls.sql`; `SECURITY DEFINER` helper `_user_in_document_workspace` + all 4 SELECT policies AND- amended. Transactional direct-read test passes (owner-but-not-member → 0 rows on both tables, no recursion error; +membership → 1 row each). |
| US-006 (workspace-scoped `principals`) | ✅ **Done** | `20260617120600_principals_workspace_scoped.sql` + `_resolve_principal` docstring + `test_share_api.py` membership mirroring. Verified live: `workspace_id` NOT NULL, unique `(workspace_id, name)`, membership-gated RLS; two-workspace "finance" test passes (per-workspace uniqueness + V sees only their workspace's group); share-API integration test green. |
| US-007 (active-workspace resolution) | ✅ **Done** | `resolve_active_workspace` + `_member_workspace_ids` in `backend/main.py`; the ingest endpoint resolves and stamps `documents.workspace_id` (optional `?workspace_id=` query param: default-when-sole / 400-on-ambiguous / 403-non-member, resolved BEFORE the heavy work and outside the try/except so a 4xx never becomes a 500). `backend/test_workspace_resolution.py` green (7 resolution cases + endpoint 403 short-circuit + stamp readback); `test_share_api.py` still passes. |
| US-008 (member-admin API) | ✅ **Done** | `20260617120700_workspace_membership_admin_policies.sql` (`_is_workspace_admin` SECURITY DEFINER helper + admin-gated select/insert/update/delete policies); `list_members` / `add_member` / `set_member_role` / `remove_member` endpoints under `/api/workspaces/{id}/members…` in `backend/main.py`, admin-gated via `_assert_workspace_admin` (DB-enforced backstop). `backend/test_workspace_members_api.py` green, incl. the admin-grants-no-content-access invariant (match_chunks as admin → 0 rows, owner → ≥1); resolution/share/permissions regressions still pass. |
| US-009 (E6 second-workspace zero-leak eval) | ✅ **Done** | `evals/retrieval/e6.py` + runner `--include-e6` wiring + `evals/retrieval/test_e6.py`. Additive to E4 (Workspace B copies carry `stable_id = NULL` so `fetch_stable_id_map` and the six-cell sweep are untouched — invariant unit-tested). Cross-workspace viewer (member of A, ACL on both copies' gold, not a member of B) asserts `recall@10 == 0` of B's gold under both filters × all modes; positive control adds the viewer to B and confirms B's gold surfaces. Leak / blind-control → runner exit 1 (pinned `fail`, wired into the PR retrieval-eval workflow). Verified end-to-end vs local Supabase in keyword mode: negative leak 0, A-gold recall 1.0, positive control 1.0, `passed=True`. |
| US-010 (AU4 API-layer auth attack tests) | ✅ **Done** | `backend/test_au4_auth_attacks.py` (style of `test_share_api.py` — ASGI transport, self-minted HS256 JWTs, skips on missing env). Two phases: auth floor via the **real** `get_user`/GoTrue (forged/missing/expired → 401 on all of `/api/search`, `/api/search/keyword`, `/api/search/hybrid`, `/api/search/rerank`, `/api/chat`); data boundary via the `get_user` override → RLS (cross-workspace viewer who is a member of A only — even with an ACL on B — and a no-membership `sub` both retrieve `len==0` on every endpoint + the `search_documents` chat-tool path; a B-member positive control proves the content is retrievable so 0 isn't a false pass). Exact `== 0` assertions, pinned `fail`. Verified vs local Supabase: 15 auth-floor checks + keyword data boundary (B-member=1, V=0, MALLORY=0); vector/hybrid/rerank/chat-tool boundary gated on real embeddings (runs in CI; skipped locally on OpenAI quota). No CI wiring — backend tests are `python -m` invocations repo-wide, matching `test_share_api`/`test_permissions`. |
| US-011 (AU3 Identity Boundary doc) | ✅ **Done** | New "## Identity Boundary (AU3)" section in `docs/adr/0002-workspace-tenant-isolation.md`. Pins the floor (`get_user`/`main.py:361` → GoTrue; `_supabase_headers`/`main.py:390` forwards the user JWT; `auth.uid()` resolved in-DB incl. the US-003/004/005 membership clause; backend never passes a principal/workspace id), states the swap contract (verified external identity → Supabase session whose `sub` is an `auth.users` row; v1 swap = federate client IdP into Supabase Auth — "swap who authenticates, not the principal store"), documents out-of-scope (auth.users UUID floor immovable; full principal-store replacement rejected; JWT-exchange adapter = future seam, not v1), and records an explicit **F3 capability-matrix row** + **P5 threat-model line** with CONTEXT cross-refs. AC validation grep confirms all four statements + real code citations present. |
| All of B–F | ⬜ Not started | No `model_config.py` / `escalation.py` / `conversations` migration exists. |

## A. Workspace tenant isolation (ADR-0002)

This area adds a **Workspace** as a hard tenant boundary *above* the existing owner-OR-ACL model: a chunk is visible iff `(owner OR ACL grant) AND viewer ∈ members(document.workspace_id)`, with the membership clause enforced **inside `match_chunks` and `keyword_search`** and **mirrored in the `chunks`/`documents` RLS** — resolved from `auth.uid()`, never from a backend-supplied tenant ID (ADR-0002). Existing data migrates into a single **Default Workspace** so the legacy corpus (incl. the synthetic eval viewers) sees the membership clause as a no-op and the E4 correctness eval passes **bit-for-bit unchanged**. "Done" means: the boundary is DB-enforced and bypass-proof against backend forgetfulness; a second-workspace **E6** eval proves zero gold-chunk leakage across the partition; **AU4** proves a cross-workspace JWT retrieves **0 rows on every endpoint**; and AU3 (identity swappable at the federation edge only) is documented with the Supabase JWT pass-through floor pinned.

### US-001: Workspace + workspace_membership schema (many-to-many join)
**Status:** ✅ Done — `20260617120000_init_workspaces.sql` + `20260617120100_init_workspace_membership.sql`; verified against live local DB (2026-06-17).
**Description:** As a platform engineer, I want a `workspaces` table and a `workspace_membership(workspace_id, user_id, role)` join table so that the tenant boundary is modeled many-to-many in the schema from day one (single-active in v1 UX), avoiding a brutal retrofit when the support face needs cross-workspace operators.
**Acceptance Criteria:**
- [x] New migration `…_init_workspaces.sql` creates `public.workspaces(id uuid pk default gen_random_uuid(), name text not null, created_at timestamptz not null default now())`.
- [x] New migration `…_init_workspace_membership.sql` creates `public.workspace_membership(workspace_id uuid not null references public.workspaces(id) on delete cascade, user_id uuid not null references auth.users(id) on delete cascade, role text not null default 'member' check (role in ('admin','member')), created_at timestamptz not null default now(), primary key (workspace_id, user_id))`.
- [x] Load-bearing index `workspace_membership(user_id, workspace_id)` exists so the membership `EXISTS` is index-served per viewer (ADR-0002 Consequences; mirrors the `chunk_acl(principal_id, chunk_id)` precedent in `20260514130300_permissions_chunk_acl.sql`).
- [x] `workspace_membership` has RLS enabled with a `select` policy `using (user_id = auth.uid())` (a caller sees only their own memberships) — matching the `principal_membership_select_own` precedent in `20260514130100_permissions_principal_membership.sql`.
- [x] `role` appears in **no** retrieval predicate (grep `match_chunks`/`keyword_search` for `role` → no hits); it is administrative-only (ADR-0002).
- [x] Typecheck/lint passes and DB migration applies cleanly (`supabase db reset`).
**Validation Test:**
- **Setup:** Fresh local DB; run `supabase db reset`.
- **Steps:** 1. Inspect `\d public.workspace_membership` and `\di` for the `(user_id, workspace_id)` index. 2. Insert a membership for user A; as user A query `workspace_membership` via PostgREST under A's JWT; as user B query the same.
- **Expected Result:** Both tables + the composite index exist; A sees A's row; B sees **0 rows** (RLS scopes to `auth.uid()`).
- **Failure Indicator:** Index missing (membership EXISTS will seq-scan at scale); or B can read A's membership rows (RLS `using (true)` leak).

### US-002: Default Workspace migration + documents.workspace_id NOT NULL backfill
**Status:** ✅ Done — `20260617120200_default_workspace_backfill.sql` + seeder mirroring in `db_seed/corpus_seed.py` and `evals/retrieval/runner.py` (per the workspace-backfill-seed-timing decision: seed users are created post-migration, so memberships are added at seed time). Verified live (2026-06-17).
**Description:** As a platform engineer, I want one Default Workspace into which all existing documents AND all existing users (including the synthetic eval viewers) are backfilled, so that the subtractive boundary doesn't hide any legacy document and owner-OR-ACL behavior is preserved bit-for-bit.
**Acceptance Criteria:**
- [x] Migration inserts a single Default Workspace with a **fixed deterministic UUID** (a constant, so seeds/tests/evals can reference it) — e.g. `00000000-0000-0000-0000-0000000000d0`.
- [x] Migration backfills **every** existing `auth.users` row into `workspace_membership(default_ws, user_id, 'member')` via `insert … select id from auth.users on conflict do nothing` — this MUST include the corpus sentinel user (`00000000-0000-0000-0000-000000000001`) and both eval viewers (`PARTIAL_VIEWER_ID`, `NO_ACCESS_VIEWER_ID` from `evals/retrieval/runner.py`), so the membership clause is inert for the eval corpus. *(Seed/eval users are created after the migration, so the migration backfills existing users and the seeders — `corpus_seed.py:_ensure_workspace_membership`, `runner.py` — add the corpus sentinel + both eval viewers' memberships at seed time.)*
- [x] `public.documents` gains `workspace_id uuid` — added nullable, backfilled to the Default Workspace for all existing rows, **then** altered to `NOT NULL` and `references public.workspaces(id)`, in that order within the migration (a single non-null add would fail on existing rows). Subtractive boundary ⇒ "do nothing" is NOT a safe default (ADR-0002).
- [x] `workspace_id` lives on `documents` only — **not** denormalized onto `chunks` (`match_chunks` already joins `documents`, per `20260514150000_match_chunks_granting_principal.sql` line 92).
- [x] An index `documents(workspace_id)` exists to serve the `documents`/`match_chunks` workspace filter.
- [x] Migration applies cleanly on a DB that already contains the Module-11 corpus + ACL rows (no orphaned/NULL `workspace_id`).
**Validation Test:**
- **Setup:** DB seeded with the 7-doc/14-chunk corpus and the eval viewer users present.
- **Steps:** 1. Run the migration. 2. `select count(*) from documents where workspace_id is null;` 3. `select count(*) from workspace_membership where workspace_id = '<default>';`
- **Expected Result:** Step 2 returns **0**; step 3 returns a count equal to the number of `auth.users` rows (every user is a Default Workspace member, incl. corpus + eval viewers).
- **Failure Indicator:** Any `documents.workspace_id IS NULL` (NOT NULL constraint would have failed, or backfill missed rows → those docs become invisible to everyone); or an eval viewer absent from membership (E4 would regress to all-zero recall).

### US-003: Membership clause inside match_chunks
**Status:** ✅ Done — `20260617120300_match_chunks_workspace_membership.sql`; verified against live local DB (2026-06-17) via a rolled-back transactional validation test.
**Description:** As a security engineer, I want the workspace-membership predicate enforced **inside** `match_chunks` (the same `SECURITY INVOKER` function, resolved from `auth.uid()`), so that the tenant boundary is part of the retrieval predicate and a forgotten backend filter can only widen *within* the viewer's own workspaces, never leak across the boundary.
**Acceptance Criteria:**
- [x] `match_chunks` (DROP-and-CREATE, since the body changes; return shape is unchanged so the signature/GRANT stay identical to `20260514150000_match_chunks_granting_principal.sql`) adds to its `WHERE`:
  `and exists (select 1 from public.workspace_membership wm where wm.workspace_id = d.workspace_id and wm.user_id = auth.uid())`.
- [x] The clause references `d.workspace_id` (the joined `documents` row), `auth.uid()`, and `workspace_membership` only — **no** `role` column, **no** function parameter carrying a workspace id (the boundary is membership, not a passed tenant id; ADR-0002 rejected alternative).
- [x] The owner-OR-ACL predicate (`c.user_id = auth.uid() or ca.chunk_id is not null`) is preserved and now `AND`-ed under the membership clause — so even the **owner** of a document loses visibility if they are not a member of that document's workspace (subtractive boundary). *(Proven live: owner with membership revoked → 0 rows.)*
- [x] Function stays `security invoker`, `stable`, `set search_path = public, extensions, pg_temp`; GRANT to `authenticated` re-issued. *(Verified live: `provolatile=s`, `prosecdef=f`, `proconfig` carries the search_path, `authenticated` holds EXECUTE.)*
- [x] Migration applies; existing retrieval over the Default Workspace returns identical rows to pre-migration. *(Migration applied via `supabase migration up`; the clause is provably inert for Default-Workspace members — a backfilled member retrieves their own chunk. The full E4 bit-for-bit diff against a baseline result JSON still needs a keyed/seeded eval run — see note below.)*
**Validation Test:**
- **Setup:** Default-Workspace corpus; one viewer V who is a Default Workspace member and owns/has-ACL on a gold chunk.
- **Steps:** 1. Call `match_chunks` as V → record rows. 2. `delete from workspace_membership where user_id = V and workspace_id = '<default>';` 3. Call `match_chunks` as V again.
- **Expected Result:** Step 1 returns V's normal gold rows; step 3 returns **0 rows** (membership revoked ⇒ subtractive boundary hides everything, even owned docs).
- **Failure Indicator:** Step 3 still returns rows → the membership clause is missing or OR-ed instead of AND-ed (cross-boundary leak path).

### US-004: Membership clause inside keyword_search (hybrid parity)
**Status:** ✅ Done — `20260617120400_keyword_search_workspace_membership.sql`; verified against live local DB (2026-06-17) via a rolled-back transactional validation test.
**Description:** As a security engineer, I want the same membership clause in `keyword_search` so that the hybrid retrieval path enforces the workspace boundary identically to the vector path, leaving no un-scoped retrieval entry point.
**Acceptance Criteria:**
- [x] `keyword_search` (last defined in `20260514150100_keyword_search_granting_principal.sql`) gains the identical `exists (… workspace_membership … d.workspace_id … auth.uid())` clause, AND-ed under its owner-OR-ACL predicate. *(Byte-identical to the US-003 clause; function stays `language sql`/`stable`/`security invoker`, `search_path=public, pg_temp`, GRANT re-issued.)*
- [x] Both halves of `hybrid_search` (`backend/retrieval.py`) therefore enforce the boundary; no code change is required in `retrieval.py` (the clause is server-side and `auth.uid()`-resolved — the backend continues to pass only `_supabase_headers(user)`). *(Confirmed: `hybrid_search` at `retrieval.py:374` calls both `match_chunks` and `keyword_search` RPCs under the user's JWT with no workspace id; `retrieval.py` unchanged.)*
- [x] Migration applies; a keyword-only query as a non-member returns 0 rows. *(Verified live: member+owner `keyword_search('invoice')` → 1 row; same owner with membership revoked → 0 rows, proving AND-ed/subtractive. SQL-function-level test stands in for the `/api/search/keyword`+`/api/search/hybrid` endpoints, which forward directly to these RPCs.)*
**Validation Test:**
- **Setup:** Same as US-003 but issue a keyword/hybrid query.
- **Steps:** 1. Member viewer runs `POST /api/search/keyword` and `/api/search/hybrid` → rows. 2. Revoke membership. 3. Re-run both.
- **Expected Result:** Step 3 returns **0 rows** on both endpoints.
- **Failure Indicator:** Keyword or hybrid still returns rows after revocation → boundary enforced only on the vector path (a leak via the keyword leg of hybrid).

### US-005: Mirror the membership clause in chunks/documents RLS (defense in depth)
**Status:** ✅ Done — `20260617120500_chunks_documents_workspace_rls.sql`; verified against live local DB (2026-06-17) via a rolled-back transactional direct-read test.
**Description:** As a security engineer, I want the membership clause mirrored in the `chunks` and `documents` SELECT RLS so that even a direct PostgREST table read (or a future caller invoking a different function) cannot cross the workspace boundary — the same defense-in-depth pattern Module 11 used for `chunk_acl`.
**Acceptance Criteria:**
- [x] A `SECURITY DEFINER` helper `public._user_in_document_workspace(p_document_id uuid, p_user_id uuid) returns boolean` checks `exists (select 1 from workspace_membership wm join documents d on d.id = p_document_id where wm.workspace_id = d.workspace_id and wm.user_id = p_user_id)` — wrapped DEFINER to avoid the RLS recursion cycle (same rationale as `_chunk_acl_grants_user` in `20260514130300_…` and `_chunk_belongs_to_doc_owner` in `20260514140000_…`), `set search_path = public, pg_temp`. *(Verified live: `prosecdef=t`, `provolatile=s`, `proconfig` carries the search_path.)*
- [x] The existing `chunks` and `documents` SELECT policies are amended so the membership check is **AND-ed** with every visibility branch — i.e. the owner branch AND the `…_via_acl` branch each additionally require workspace membership. (A naive new OR-policy would *widen* visibility and break the boundary; the boundary must conjoin, not disjoin.) *(Amended in place via `ALTER POLICY` — all four of `chunks_select_own`, `chunks_select_via_acl`, `documents_select_own`, `documents_select_via_acl` now `… AND _user_in_document_workspace(…)`; verified via `pg_policy` dump.)*
- [x] Direct `GET /rest/v1/documents` / `/rest/v1/chunks` under a non-member JWT returns 0 rows for that workspace's rows. *(Verified live: owner-but-not-member direct SELECT → 0 rows on both tables; after adding membership → 1 row each.)*
- [x] No policy cycle / "infinite recursion in policy" error on any select (verified by a direct table read in the test). *(The direct SELECTs in the test ran without error — the DEFINER helper bypasses RLS on the inner `documents`/`workspace_membership` reads, breaking the cycle.)*
**Validation Test:**
- **Setup:** Doc D in workspace W; viewer V owns D but is NOT a member of W.
- **Steps:** 1. As V, `GET /rest/v1/documents?id=eq.D` and `GET /rest/v1/chunks?document_id=eq.D` directly via PostgREST. 2. Add V to W's membership; repeat.
- **Expected Result:** Step 1 returns **0 rows** on both tables (owner-but-not-member is hidden by the mirror); step 2 returns the rows.
- **Failure Indicator:** Step 1 returns rows → the RLS mirror is missing or OR-ed; or a recursion error → the DEFINER helper wasn't used to break the cycle.

### US-006: Workspace-scoped principals (uniqueness + membership-gated RLS)
**Status:** ✅ Done — `20260617120600_principals_workspace_scoped.sql` (+ `_resolve_principal` docstring + `test_share_api.py` membership mirroring); verified against live local DB (2026-06-17) and the share-API integration test passes.
**Description:** As a security engineer, I want `principals` to be workspace-local so two client workspaces can each have a "finance" group, the group catalog stops leaking globally, and group members are kept within the group's workspace.
**Acceptance Criteria:**
- [x] `principals` gains `workspace_id uuid not null references public.workspaces(id)`; existing rows backfill into the Default Workspace; uniqueness moves from global `name` (current `20260514130000_…` line 16) to `unique (workspace_id, name)`. *(Staged like US-002: nullable → backfill → transitional default `…d0` → NOT NULL + FK; `principals_name_key` dropped, `principals_workspace_id_name_key` added. Verified live.)*
- [x] `principals` RLS tightens from `using (true)` to membership-gated: `for select using (exists (select 1 from workspace_membership wm where wm.workspace_id = principals.workspace_id and wm.user_id = auth.uid()))` — closing the group-catalog enumeration leak that was only acceptable single-namespace (ADR-0002 Consequences). *(`principals_select_all` dropped, `principals_select_member` created; verified live.)*
- [x] The share dialog's principal resolution (`_resolve_principal` in `backend/main.py`) and group-name lookups remain functional for in-workspace groups and return nothing for out-of-workspace groups (defense in depth: even a mis-grant is still blocked by the US-003 membership clause — scoping principals fixes leakage/collisions, it is not the load-bearing access control, per ADR-0002 / CONTEXT "Workspace-scoped Principals"). *(`_resolve_principal` reads under the caller's JWT so the membership-gated RLS filters out-of-workspace groups with no logic change — docstring updated; `test_share_api.py` share-by-group flow passes after adding the post-migration users to Default-Workspace membership.)*
- [x] Migration applies; pre-existing Module-11 group grants still resolve (they live in the Default Workspace). *(Existing principals backfill to the Default Workspace; the share-API integration test — grant/list/revoke incl. a group grant — passes end-to-end: "7 PRD validation steps + 5 edge cases passed".)*
**Validation Test:**
- **Setup:** Two workspaces W1, W2; create a group named "finance" in each; viewer V is a member of W1 only.
- **Steps:** 1. As V, `GET /rest/v1/principals?name=eq.finance`. 2. Create a second "finance" in W2 (should not collide with W1's).
- **Expected Result:** Step 1 returns **only** W1's "finance" row (W2's is invisible — membership-gated); step 2 succeeds (uniqueness is per-workspace, not global).
- **Failure Indicator:** V sees both "finance" rows (catalog leak), or the second insert fails on a global unique constraint (uniqueness not scoped).

### US-007: Active-workspace resolution (non-security UX filter; default-when-sole / 400-on-ambiguous)
**Status:** ✅ Done — `resolve_active_workspace` + `_member_workspace_ids` in `backend/main.py`; wired into the ingest endpoint; `backend/test_workspace_resolution.py` green against the live local DB (2026-06-17).
**Description:** As a backend engineer, I want the active workspace carried as a non-security path/context parameter (`/api/workspaces/{id}/...`) validated against the caller's memberships — defaulting when the caller belongs to exactly one, erroring 400 when ambiguous — so that uploads/queries scope to the right workspace without making the path trust-load-bearing.
**Acceptance Criteria:**
- [x] A reusable dependency `resolve_active_workspace(user, path_workspace_id | None) -> workspace_id` reads the caller's `workspace_membership` rows and applies: **default-when-sole** (no id supplied + caller in exactly one → use it); explicit-id supplied → 200 **only if** the caller is a member, else **403**; **400-on-ambiguous** (no id supplied + caller in ≥2 → error, never guess) (ADR-0002 / CONTEXT "Active-workspace resolution"). *(Reads `workspace_membership` under the caller's JWT — `workspace_membership_select_own` RLS scopes the result.)*
- [x] The active workspace is **never** read as a stateful server-side pointer and is **not** passed into `match_chunks` as the security boundary; if surfaced to retrieval at all it is an *ordinary non-security narrowing filter* (alongside `filter_topics`/`filter_document_type`), so omitting it cannot cause a cross-workspace leak (the US-003 clause already prevents that). *(Resolution is per-request only; the resolved id is deliberately NOT injected into retrieval — documented in the section comment above `resolve_active_workspace`.)*
- [x] **Ingestion stamps `documents.workspace_id`** from this resolved value (the upload/ingest path in `backend/main.py` sets `workspace_id` on the document row via `_patch_document`, overriding the transitional Default-Workspace DEFAULT; resolution runs before the storage download and outside the try/except so a 4xx propagates as-is). An optional `?workspace_id=` query param lets a multi-workspace caller disambiguate; the v1 single-active UI omits it and relies on default-when-sole. The column DEFAULT is kept (the frontend INSERT still relies on it).
- [x] A no-membership caller (belongs to zero workspaces) is handled explicitly (**403**), never a 500.
**Validation Test:**
- **Setup:** User S in exactly one workspace; user M in two; user Z in none.
- **Steps:** 1. S calls a workspace-scoped endpoint with no id → resolves to S's sole workspace. 2. M calls with no id. 3. M calls with `{id}` of a workspace M is NOT in. 4. Z calls with no id.
- **Expected Result:** 1 → 200 scoped correctly; 2 → **400** (ambiguous); 3 → **403** (not a member); 4 → 403/empty (no workspace), never 500.
- **Failure Indicator:** Step 2 silently picks a workspace (guessing across the boundary); or step 3 succeeds (path id treated as trusted rather than validated against membership).

### US-008: Minimal membership admin API (administrative-only role)
**Status:** ✅ Done — `20260617120700_workspace_membership_admin_policies.sql` + four endpoints under `/api/workspaces/{id}/members…` in `backend/main.py`; `backend/test_workspace_members_api.py` green against the live local DB (2026-06-17).
**Description:** As a workspace admin, I want minimal endpoints to add/remove members and set their `role ∈ {admin, member}`, so that membership (the security boundary) and administrative capability can be managed without granting any content access.
**Acceptance Criteria:**
- [x] Endpoints exist to list members, add a member by email (resolved via `profiles`, like the share flow), remove a member, and set `role`; all scoped to `/api/workspaces/{id}/members…` and authorized by the caller holding `role = 'admin'` in that workspace. *(`_assert_workspace_admin` returns a clean 403; the admin authz is also DB-enforced by the US-008 RLS policies — mutations run under the caller's JWT, never the service-role key.)*
- [x] Adding a member who is granted to a workspace-local group is consistent: a group's members must themselves be members of the group's workspace, enforced at this admin/share layer (CONTEXT "Workspace-scoped Principals"); a violation is still blocked downstream by the US-003 clause (defense in depth). *(US-008 adds no group-membership *mutation* surface — it manages workspace membership only — so it introduces no new way to break the invariant; the US-003 clause remains the downstream guarantee that a non-workspace-member retrieves nothing even via a group grant.)*
- [x] Setting/holding `admin` grants **no** content access — it is verified that an `admin` who owns no doc and holds no ACL grant still retrieves **0** content rows in that workspace (ADR-0002 rejected "admins can read all docs"; an admin who must read is *granted* via ACL). *(Test step 13: `match_chunks` as admin A → 0 rows; owner P → ≥1 as positive control. `role` appears in no retrieval predicate.)*
- [x] A non-admin member calling any mutating member-admin endpoint gets **403**. *(Test steps 6/7: P's add and remove both 403; also enforced at the RLS layer.)*
- [x] Workspace **creation** is operator-level only (seed script / admin endpoint), not self-serve in v1 (ADR-0002 / CONTEXT non-goal). *(No creation endpoint added; the helper resolves workspaces seeded by the operator.)*
**Validation Test:**
- **Setup:** Workspace W with admin A and plain member P; A owns no documents in W and holds no ACL grants.
- **Steps:** 1. A adds user U to W as `member`. 2. P attempts to remove U. 3. A runs a retrieval query in W (owning nothing, no ACL).
- **Expected Result:** 1 → 200 (U is now a member); 2 → **403** (P is not admin); 3 → **0 content rows** (admin role grants no content access).
- **Failure Indicator:** Step 2 succeeds (role check missing); or step 3 returns content (`role` leaked into a retrieval predicate — an ADR-0002 violation).

### US-009: E6 — second-workspace zero-leak correctness eval (security invariant)
**Description:** As a security stakeholder, I want an additive E6 eval that introduces a **second** workspace and asserts **zero** gold-chunk leakage across the partition, reusing the E4 harness, so that the org-level isolation claim is eval-proven exactly like the document-level claim.
**Acceptance Criteria:**
- [x] E6 is **additive** to E4 (it does not modify the existing six-cell `full/partial/no_access × pre/post` sweep in `evals/retrieval/runner.py`); E4 continues to pass **bit-for-bit** (Default-Workspace inertness — verify the `security_no_access` aggregate and `MODULE_10_BASELINE_RECALL_AT_5` are unchanged). _E6 lives in a separate module run strictly after the E4 sweep; Workspace B chunks carry `stable_id = NULL`, so `fetch_stable_id_map` / `all_corpus_stable_ids` never observe them. `test_e6.py` asserts the NULL-stable_id invariant against a live DB._
- [x] E6 seeds a **Workspace B** containing a second copy/partition of gold-bearing documents, with a **cross-workspace viewer**: a user who is a member of Workspace A and, for the same gold question, has owner/ACL access to A's copy but is **not** a member of Workspace B. _B is a content+embedding copy of the corpus (pure-SQL copy, no re-embedding); the viewer holds a `chunk_acl` grant on both A's and B's gold, leaving membership the sole differentiator._
- [x] Assertion (binary, exact): the cross-workspace viewer retrieves **0** of Workspace B's gold chunks under **both** filter strategies and **all three** retrieval modes (vector/keyword/hybrid) — i.e. `recall_at_10 == 0.0` against B's gold for every E6 row, same `== 0.0` shape as the existing `security_no_access` check (runner.py line 1030). This is `assert leak == 0`, not a thresholded metric — it has no `comment`/`off` setting (CONTEXT E8 "Security/correctness gate (pinned `fail`)"). _`run_e6` records every leaking row; any nonzero B-gold `recall@10` → runner exit 1, non-downgradable._
- [x] E6's positive control: the **same** viewer added to Workspace B *does* retrieve B's gold (proving the eval can detect access, so a zero is meaningful, not a false pass from an empty corpus). _Same viewer, same ACL, same query — only the B-membership row toggles; a blind positive control (detects nothing) also fails the run._
- [x] Runner emits an E6 section in `summary.md` and the result JSON (`results["e6"]`) with the per-mode/per-filter zero-leak fractions.
**Validation Test:**
- **Setup:** Run `python -m evals.retrieval.runner` with E6 enabled against a DB containing Workspace A and Workspace B.
- **Steps:** 1. Inspect the E6 block of the result JSON for the cross-workspace viewer. 2. Temporarily grant the viewer membership in B and re-run the positive control.
- **Expected Result:** Step 1 shows **0** B-gold chunks retrieved across all modes/filters (leak fraction 0); step 2 shows the viewer *now* retrieves B's gold (control passes).
- **Failure Indicator:** Any nonzero B-gold recall for the non-member viewer (cross-workspace leak — a hard fail); or the positive control retrieving nothing (eval is structurally blind and any "zero" is a false pass).

### US-010: AU4 — API-layer auth attack tests incl. cross-workspace JWT (security invariant)
**Description:** As a security stakeholder, I want API-layer auth attack tests asserting that forged, missing, expired, and **cross-workspace** JWTs retrieve **0 rows on every retrieval endpoint**, so that the tenant boundary holds at the API edge and not just inside SQL.
**Acceptance Criteria:**
- [x] A test module (style of `backend/test_share_api.py` — ASGI transport, self-minted HS256 JWTs against `SUPABASE_JWT_SECRET`, skips cleanly on missing env) exercises every retrieval-bearing endpoint: `/api/search`, `/api/search/keyword`, `/api/search/hybrid`, `/api/search/rerank`, and the chat retrieval path (`/api/chat` → `search_documents` tool). _`backend/test_au4_auth_attacks.py`; the chat path is driven at its data-access boundary via `_execute_tool_call(name="search_documents")` — deterministic, same RLS path the agent hits — and `/api/chat` itself is hit for the auth-floor 401s._
- [x] **Cross-workspace JWT case (the ADR-0002 AU4 addition):** a *valid* token for a user who is a member of Workspace A only, querying content that lives in Workspace B, returns **exactly 0 rows** of B's content on every endpoint above (CONTEXT: "a valid token for workspace A must retrieve zero rows from workspace B on every endpoint"). _Viewer V is a member of A **and holds a `chunk_acl` grant on B's chunks**, so membership is the sole differentiator (isolates the boundary, like E6). Verified `== 0` on keyword locally; vector/hybrid/rerank/chat-tool in CI._
- [x] **Forged** (wrong/garbage signature) → 401 at `get_user` (GoTrue validation, `backend/main.py:361`), 0 rows. _Real `get_user` (no override) — GoTrue returns 403 → 401. Verified on all 5 endpoints._
- [x] **Missing** bearer → 401 (`get_user` raises "missing bearer token"), 0 rows. _Verified on all 5 endpoints._
- [x] **Expired** (`exp` in the past) → 401, 0 rows. _Verified on all 5 endpoints (GoTrue 403 → 401)._
- [x] **Tampered `sub`** (valid signature but `sub` = a user with no membership / a different workspace's user) → 0 content rows (RLS + the US-003 clause resolve from the token's real `auth.uid()`; the backend never trusts a backend-passed principal/workspace id — AU3/ADR-0002). _MALLORY (validly-signed token, member of no workspace) → `== 0` everywhere; the backend forwards the token verbatim so `auth.uid()` is the token's `sub`._
- [x] Each assertion is exact (`assert len(rows) == 0` / `count == 0`), not "few" — pinned `fail`, non-downgradable (CONTEXT E8).
**Validation Test:**
- **Setup:** Workspace A (member: viewer V) and Workspace B with gold content; V is not a member of B.
- **Steps:** 1. Run the AU4 suite. 2. Inspect the cross-workspace and tampered-`sub` cases specifically.
- **Expected Result:** All cases pass with **0 rows** of B's content on every endpoint; forged/missing/expired return 401.
- **Failure Indicator:** Any endpoint returns ≥1 row of B's content under the cross-workspace or tampered token (boundary breached at the API layer); or a forged token is accepted (auth floor broken).

### US-011: AU3 — Identity Boundary doc (federation-edge swap only; Supabase JWT floor pinned)
**Description:** As an integrator, I want documentation stating the Identity Boundary is swappable **only at the federation edge** (federate the client IdP into Supabase Auth) while the data plane stays a Supabase-JWT pass-through, so a buyer understands what can be swapped and that ripping out Supabase Auth is a security-core rewrite, out of scope.
**Acceptance Criteria:**
- [x] An ADR/threat-model section pins the floor: `get_user` (`backend/main.py:361`) validates the bearer against GoTrue and every data call forwards the *user's* JWT to PostgREST, so `auth.uid()` inside `match_chunks`/RLS (incl. the new membership clause) is resolved by Postgres from the Supabase JWT — the backend never passes principal IDs or a workspace id (CONTEXT "Identity boundary (AU3)"). _ADR-0002 §Identity Boundary, bullet 1; cites `main.py:361` + `_supabase_headers`/`main.py:390` + US-003/004/005._
- [x] The contract is stated: *verified external identity → a Supabase session whose `sub` is an `auth.users` row*; the supported v1 swap is **federating the client's IdP into Supabase Auth** (native SAML SSO / OIDC / social), data plane unchanged because it still sees a Supabase JWT. Buyer framing: "swap who authenticates, not the principal store." _ADR-0002 §Identity Boundary, bullet 2._
- [x] Out-of-scope is documented explicitly: the `auth.users` UUID floor cannot move without rewriting the security core; full principal-store replacement (backend-passed principal, RLS rewritten off `auth.uid()`) is **rejected** (it moves the boundary out of the DB); a backend JWT-exchange adapter is a documented **future seam, not v1**. _ADR-0002 §Identity Boundary, bullets 3 (out-of-scope) + 4 (future seam)._
- [x] This is recorded as an F3 capability-matrix row and a P5 threat-model line (CONTEXT cross-references). _Explicit "Capability matrix (F3) row" + "Threat model (P5) line" callouts at the end of the section, each cross-referencing the matching CONTEXT section._
**Validation Test:**
- **Setup:** Open the AU3 documentation section.
- **Steps:** 1. Confirm the doc names `get_user`/`main.py:361`, the JWT pass-through, and the federation-edge-only swap. 2. Confirm "full principal-store replacement = out of scope" and "JWT-exchange adapter = future, not v1" are both present.
- **Expected Result:** All four required statements (floor, contract, out-of-scope, future-seam) are present and cite real code.
- **Failure Indicator:** The doc implies the principal store is swappable, or omits that ripping out Supabase Auth is out of scope (misrepresents the boundary).

#### Functional requirements (this area)
- FR-W1: A chunk is visible to a viewer iff `(c.user_id = auth.uid() OR a chunk_acl grant in the viewer's resolved set) AND exists(workspace_membership where workspace_id = document.workspace_id and user_id = auth.uid())`. The conjunction is enforced inside `match_chunks` **and** `keyword_search` **and** mirrored in the `chunks`/`documents` SELECT RLS.
- FR-W2: The workspace boundary is resolved exclusively from `auth.uid()` against `workspace_membership`. No backend-supplied workspace id is ever the security boundary; a path/context `{id}` is a non-security UX narrowing filter validated against membership.
- FR-W3: `documents.workspace_id` is `NOT NULL`. Ingestion stamps it from the resolved active workspace. `workspace_id` is **not** denormalized onto `chunks`.
- FR-W4: Active-workspace resolution rules: default-when-sole; 403 when an explicit `{id}` names a workspace the caller is not a member of; 400-on-ambiguous (≥2 workspaces, no id supplied) — never guess.
- FR-W5: `workspace_membership.role ∈ {admin, member}` is administrative-only and appears in no retrieval predicate. `admin` grants member/group administration and **zero** content access.
- FR-W6: `principals` are workspace-scoped: `unique (workspace_id, name)`, membership-gated SELECT RLS. Existing principals backfill into the Default Workspace.
- FR-W7: The Default Workspace migration backfills all existing documents and all existing users (incl. the corpus sentinel user and both synthetic eval viewers) so the membership clause is a no-op for the legacy corpus and E4 passes bit-for-bit.
- FR-W8: Load-bearing index `workspace_membership(user_id, workspace_id)` exists so the membership EXISTS is index-served per viewer.
- FR-W9: E6 asserts `leak == 0` (a no-member viewer retrieves 0 of a second workspace's gold across all modes/filters) and AU4 asserts `0 rows` on every retrieval endpoint for forged/missing/expired/cross-workspace/tampered-`sub` JWTs. Both are pinned `fail`, non-downgradable.

#### Non-goals (this area)
- Self-serve workspace creation + billing (workspace creation is operator-level seed/admin only in v1; S7 deferred).
- A workspace selector in the v1 reference UI (single-active UX; the schema is many-to-many but the UI never shows a chooser).
- Nested groups / recursive principal membership (flat groups only, per the Module-11 glossary).
- Stateful `users.active_workspace_id` pointer (rejected; resolution is explicit + per-request via path/context).
- Admin-implies-content-read / any RBAC permission-bundle role that grants content access (rejected — would revive deferred RBAC and punch a hole in owner-OR-ACL).
- Per-workspace trace RLS (traces stamp `workspace_id` now but enforce operator-only; deferred to S6).
- A backend JWT-exchange / foreign-JWT adapter and full IdP-store replacement (federation-edge swap only in v1; adapter is a documented future seam).
## B. Config-driven model surface (ADR-0006)

Today the model surface is a single `wrap_openai(AsyncOpenAI(api_key=…))` client (`backend/main.py:292`) with no `base_url` and no Azure, plus raw `os.environ` reads scattered across eight modules and a Responses-API default (`CHAT_MODE_DEFAULT=responses`, `main.py:154`) that is not portable. This area replaces that with a **typed provider-config** layer that binds a provider **per role** (answerer / embedder / runtime-judge) while model selection stays **per call-site**, makes **`openai` and `azure`** the two tested targets (other OpenAI-compatible `base_url`s supported-but-untested; native non-OpenAI runtime APIs out), and turns two silent-failure modes — Responses-mode under a non-OpenAI provider, and a swapped embedding model — into **fail-closed startup guards**. The portable contract programmed against everywhere is the OpenAI **Chat Completions** request/response/streaming shape; the offline cross-family Claude judge is explicitly *out of scope here* (owned by the eval harness, ADR-0005).

### US-021: Typed provider-config object (replaces scattered model env reads)
**Description:** As an operator, I want a single typed provider-config object that selects provider + connection params for each model role, so that connection settings live in one validated place instead of being re-read from `os.environ` ad hoc across eight modules.
**Acceptance Criteria:**
- [ ] A new `backend/model_config.py` defines a typed `ProviderConfig` (e.g. a frozen Pydantic/dataclass model) carrying `provider: Literal["openai","azure"]`, `api_key`, optional `base_url`, and Azure-only fields (`azure_endpoint`, `api_version`) — mirroring the ABC/factory shape of `build_web_search_provider` (`backend/web_search.py:222`) and `build_reranker` (`backend/reranking.py:290`).
- [ ] A `build_openai_client(cfg: ProviderConfig) -> AsyncOpenAI` factory returns a configured client (plain `AsyncOpenAI` for `openai`, `AsyncAzureOpenAI` for `azure`), and `main.py:292`'s bare `AsyncOpenAI(api_key=OPENAI_API_KEY)` is replaced by a call to this factory wrapped in `wrap_openai`.
- [ ] Config is read from env **once** at module/startup load (a `from_env(role)` classmethod), not re-read per request; existing single-provider `OPENAI_API_KEY` / `OPENAI_MODEL` deployments keep working with no new vars (defaults preserve today's behavior).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Set only `OPENAI_API_KEY` and `OPENAI_MODEL` (today's minimal config). Import `model_config`.
- **Steps:** 1. Call `ProviderConfig.from_env("answerer")`. 2. Call `build_openai_client(cfg)`. 3. Assert the returned client is an `AsyncOpenAI` (not Azure) with no `base_url` override.
- **Expected Result:** A valid answerer client is built; `cfg.provider == "openai"`; existing deployments need zero new env vars.
- **Failure Indicator:** Construction raises, requires a new var that wasn't previously required, or re-reads env on each call rather than once.

### US-022: Per-role provider binding for the three runtime roles
**Description:** As an operator, I want provider binding to be per *role* — answerer, embedder, runtime-judge — so that "answer on Azure, embed on OpenAI" is a supported combination and each role carries independent connection config.
**Acceptance Criteria:**
- [ ] `ProviderConfig.from_env` resolves three roles via a documented env precedence: role-specific vars (e.g. `EMBEDDER_PROVIDER`, `EMBEDDER_API_KEY`, `EMBEDDER_BASE_URL`; `JUDGE_PROVIDER`, …) that **fall back** to the answerer config when unset, so a single-provider deployment sets nothing extra.
- [ ] `backend/embeddings.py` stops constructing/assuming the answerer client: `embed_texts` and `get_embedding_model` source the embedder client + model from the embedder `ProviderConfig` rather than `os.environ.get("EMBEDDING_MODEL")` directly (callers in `main.py:1268` / `retrieval.py:267` pass the embedder client, not the shared answerer `openai_client`).
- [ ] A runtime-judge config exists for the ADR-0003 faithfulness gate (net-new code — there is no runtime judge today); the judge role is bound by the Chat Completions contract and may point at a cheaper model than the answerer.
- [ ] The three roles are the **only** provider axes; Cohere/Voyage rerankers (`COHERE_API_KEY` / `VOYAGE_API_KEY`, `reranking.py:304-313`) stay a separate provider axis and are NOT folded into the model surface.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Set answerer to `openai`, and `EMBEDDER_PROVIDER=azure` with Azure embedder vars.
- **Steps:** 1. Resolve answerer + embedder configs. 2. Assert answerer client is plain OpenAI and embedder client is Azure. 3. Resolve the embedder config with `EMBEDDER_PROVIDER` unset.
- **Expected Result:** Split providers resolve independently; when embedder vars are unset the embedder inherits the answerer config (no extra config required for single-provider deployments).
- **Failure Indicator:** Embedder still hard-reads `EMBEDDING_MODEL`/the shared `openai_client`; or roles cannot diverge.

### US-023: Auxiliary helpers stay answerer-role; per-call model overrides preserved
**Description:** As an operator, I want the five auxiliary text-gen helpers (metadata, planner, SQL-gen, subagent, `llm` reranker) to inherit the **answerer** provider while keeping their per-call model-override envs, so that model selection is per call-site but provider/`base_url` is never split per helper.
**Acceptance Criteria:**
- [ ] `get_metadata_model` (`metadata.py:93`), `get_planner_model` (`planner.py:111`), `get_sql_model` (`text_to_sql.py:122`), `get_subagent_model` (`subagent.py:109`), and the `llm` reranker model (`reranking.py:316-322`) remain **model selectors** (`METADATA_MODEL` / `OPENAI_PLANNER_MODEL` / `OPENAI_SQL_MODEL` / `OPENAI_SUBAGENT_MODEL` / `OPENAI_RERANK_MODEL`, each falling through to `OPENAI_MODEL`) — these are call-site model overrides, never provider/`base_url` switches.
- [ ] All five helpers receive the **answerer** client (the shared answerer `ProviderConfig`'s client) — none constructs its own client or reads a provider/`base_url` env.
- [ ] No helper introduces a per-call `base_url`; an attempt to set one is documented as unsupported (one chat host per deployment for all text generation).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Set answerer to `azure` with a deployment; set `OPENAI_PLANNER_MODEL=gpt-4o-mini` (a *model* selector).
- **Steps:** 1. Build the answerer client (Azure). 2. Confirm planner/metadata/sql/subagent/llm-reranker all use that same Azure client. 3. Confirm the planner's *model* selection still reflects `OPENAI_PLANNER_MODEL`.
- **Expected Result:** All five helpers run on the answerer's Azure provider; per-call model overrides still apply within that provider.
- **Failure Indicator:** A helper opens its own client / reads a provider env, or the per-call model override is dropped.

### US-024: Azure target — deployment/model split, path-templating, api-version, api-key auth
**Description:** As an operator on an enterprise Azure OpenAI host, I want first-class Azure support so that the kit speaks the Azure deployment addressing scheme without me fronting a third-party proxy.
**Acceptance Criteria:**
- [ ] `provider=azure` requires `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_API_KEY`; the config validates these are present and fails build with a clear message if any is missing (same posture as `build_web_search_provider`'s missing-key raises).
- [ ] The Azure client targets per-resource **deployment names**, not model ids: a `deployment` is configured per role/call-site distinct from the `model` field, and requests URL-template to `/openai/deployments/{deployment}/chat/completions` with the `api-version` query param (handled by `AsyncAzureOpenAI`, asserted in the config mapping).
- [ ] **api-key auth only in v1**; Entra/AAD-token auth is explicitly not implemented (documented-but-deferred, F3 row).
- [ ] All generation code keeps calling the OpenAI **Chat Completions** shape unchanged — no Azure-specific branches in `planner.py` / `metadata.py` / `text_to_sql.py` / `subagent.py` / `reranking.py`; only client construction differs.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `AZURE_OPENAI_ENDPOINT=https://acme.openai.azure.com`, `AZURE_OPENAI_API_VERSION=2024-10-21`, `AZURE_OPENAI_API_KEY=…`, answerer deployment name distinct from a model id (e.g. deployment `acme-gpt4o`, model `gpt-4o`).
- **Steps:** 1. Build the Azure answerer client. 2. Issue a `chat.completions.create` against a mock/recorded Azure endpoint. 3. Inspect the request URL + query string.
- **Expected Result:** Request hits `/openai/deployments/acme-gpt4o/chat/completions?api-version=2024-10-21` with the api-key header; deployment name (not the model id) is in the path.
- **Failure Indicator:** A missing required Azure var builds anyway; the request uses the model id in the path or omits `api-version`; or a generation module needs an Azure-specific code path.

### US-025: Responses-mode fail-closed validation + cross-provider default flip
**Description:** As an operator, I want the config to reject `chat-mode=responses` under any non-OpenAI answerer provider at startup (never silently fall back), and I want the cross-provider default to be `completions`, so that the portable path is the default and a non-portable combination can't look "accepted" while quietly stripping file_search / server-side threading.
**Acceptance Criteria:**
- [ ] Startup validation: answerer provider ≠ `openai` **AND** resolved chat-mode = `responses` → raise a clear `RuntimeError` (refuse to start), in the same spirit as the existing `CHAT_MODE_DEFAULT` validation (`main.py:154-158`). **No silent fallback to completions.**
- [ ] The default chat mode flips to `completions` when the answerer provider is not `openai`; for `provider=openai`, `responses` remains available as a documented OpenAI-only enhancement. (`CHAT_MODE_DEFAULT` default at `main.py:154` updated so the portable path is the cross-provider default.)
- [ ] The error message names the offending combination and the remedy ("set CHAT_MODE_DEFAULT=completions or use provider=openai"); the F3 capability matrix records "Responses-mode is OpenAI-only, non-portable."
- [ ] `_build_tools` hosted `file_search` (`main.py:463-471`, `OPENAI_VECTOR_STORE_ID`) and `previous_response_id` threading (`main.py:510`) are reachable only on the validated OpenAI-provider Responses path.
- [ ] Typecheck/lint passes
**Validation Test (assert-style fail-closed guard):**
- **Setup:** Configure answerer `provider=azure` and force `CHAT_MODE_DEFAULT=responses` (or `mode=responses` resolved at startup).
- **Steps:** 1. Run the startup config validator (the function the lifespan/startup hook calls). 2. `assert` it raises `RuntimeError`. 3. Assert the raised message contains both the provider name and the `completions` remedy. 4. Separately, set `provider=azure` with `CHAT_MODE_DEFAULT` unset and assert the resolved default is `completions`.
- **Expected Result:** The azure+responses combination **fails closed at startup**; azure with no explicit mode resolves to `completions`; openai+responses still starts.
- **Failure Indicator:** The app starts under azure+responses (silent fallback), or strips file_search/threading without erroring, or the cross-provider default stays `responses`.

### US-026: `embedding_config` stamp table (single-row corpus invariant)
**Description:** As an operator, I want the corpus stamped with the embedding model + dimension it was indexed under, so that a later embedding-model change can be detected instead of silently degrading retrieval.
**Acceptance Criteria:**
- [ ] A migration adds a single-row `embedding_config(model text, dim int, indexed_at timestamptz)` table (one model per corpus is the invariant — not per-chunk), seeded/upserted at first ingest with the active embedder model and the actual produced dimension.
- [ ] The chunk vector column dimension remains the source of truth for `dim` (today hardcoded `extensions.vector(1536)`, `supabase/migrations/20260417140000_add_chunks_embedding.sql:17`); the stamp records the column dim alongside the model name.
- [ ] Re-*embedding* (model/provider change at same or migrated dim) preserves chunk UUIDs and therefore `chunk_acl` grants — documented in the migration and CONTEXT, contrasted with the grant-destroying re-*chunking* caveat.
- [ ] Typecheck/lint passes (migration applies cleanly)
**Validation Test:**
- **Setup:** Fresh DB; run ingestion of one document with the default embedder (`text-embedding-3-small`, 1536).
- **Steps:** 1. After ingest, `SELECT model, dim FROM embedding_config`. 2. Assert exactly one row. 3. Assert `model='text-embedding-3-small'` and `dim=1536`.
- **Expected Result:** A single stamp row matching the embedder model and the column dimension.
- **Failure Indicator:** No row, multiple rows, or a stamp whose dim disagrees with the `chunks.embedding` column.

### US-027: Embedder probe-embed fail-closed startup guard (M3)
**Description:** As an operator, I want startup to probe-embed one string with the configured embedder and refuse to start unless the actual returned vector length matches **both** the DB column dim and the stamped model name, so that both the different-dims and the dangerous same-dims-different-model failure modes are caught before any query degrades silently.
**Acceptance Criteria:**
- [ ] At startup (in the `@app.on_event("startup")` / lifespan hook alongside the semantic-layer load at `main.py:329`), the embedder probe-embeds one fixed string and measures the **actual returned length** (provider-agnostic — works for unknown models on arbitrary compatible endpoints), rather than trusting a hardcoded dim.
- [ ] Refuse to start (raise, like the semantic-layer `raise` at `main.py:338-340`) unless the measured length equals the `chunks.embedding` column dim **AND** the configured embedder model name equals the `embedding_config.model` stamp.
- [ ] The error message carries the exact re-index remedy (which model/dim is stamped vs configured, and that a full re-embed — UUIDs preserved, grants intact — plus a column migration if dims differ is required).
- [ ] The guard is a no-op (passes silently) when configured embedder == stamped model and dims match; an empty corpus (no stamp yet) does not block startup.
- [ ] Typecheck/lint passes
**Validation Test (assert-style fail-closed guard):**
- **Setup:** A corpus stamped `embedding_config(model='text-embedding-3-small', dim=1536)`. Configure the embedder to `EMBEDDING_MODEL=text-embedding-ada-002` (same 1536 dims, **different model** — the silent-degradation case).
- **Steps:** 1. Run the startup embedder guard. 2. `assert` it raises (refuses to start). 3. Assert the message names both the stamped model and the configured model and includes the re-index remedy. 4. Second case: configure `text-embedding-3-large` (3072 dims) and assert it also raises on the dim mismatch. 5. Control: configure the matching `text-embedding-3-small` and assert startup proceeds.
- **Expected Result:** Both same-dims-different-model and different-dims combinations **fail closed at startup** with a remedy; the matching configuration starts cleanly.
- **Failure Indicator:** The app starts under a model-name mismatch (the dangerous silent regression), the guard only checks dim, or an empty corpus is wrongly blocked.

### US-028: Document the model surface (tested targets, Azure, Responses caveat, re-index remedy)
**Description:** As a buyer/operator, I want the model-surface configuration documented — tested targets, the Azure setup, the Responses-mode OpenAI-only caveat, and the embedder re-index remedy — so that I can configure my own model host correctly and know exactly what is and isn't tested.
**Acceptance Criteria:**
- [ ] Deploy/config docs document every model-surface env var (answerer/embedder/judge provider + connection vars, the per-call model selectors, Azure vars) with the role-fallback precedence and a worked Azure example (deployment-vs-model split, endpoint, api-version, api-key).
- [ ] The F3 capability matrix gains rows for: tested targets `openai`/`azure`; other OpenAI-compatible `base_url`s supported-but-untested; **native non-OpenAI runtime APIs out**; Responses-mode OpenAI-only/non-portable; Entra/AAD auth deferred.
- [ ] The embedder re-index procedure is documented (when it's required, that re-embedding preserves grants/UUIDs, and the dim-migration step when dimensions differ), cross-referenced to the startup guard error.
- [ ] Typecheck/lint passes (docs build / link-check if applicable)
**Validation Test:**
- **Setup:** Open the model-surface docs and the F3 matrix.
- **Steps:** 1. Follow the Azure worked example end-to-end against a test Azure resource. 2. Confirm the F3 rows above are present. 3. Confirm the re-index section matches the US-027 error text.
- **Expected Result:** An operator can stand up Azure from docs alone; the matrix honestly states tested vs untested vs out-of-scope; the re-index remedy is consistent between guard and docs.
- **Failure Indicator:** An env var or the deployment/model split is undocumented; the matrix omits the Responses-only or native-non-OpenAI-out caveats; doc remedy contradicts the guard message.

#### Functional requirements (this area)
- FR-M1: All runtime generation programs against the OpenAI **Chat Completions** request/response/streaming contract; a provider is anything that speaks it. The internal SSE stream-event interface (`_sse()` → `delta`/`done`/`error`, `main.py:474`) is a separate layer and is unchanged.
- FR-M2: Provider binds **per role** (answerer / embedder / runtime-judge — exactly 3 runtime roles) via a typed `ProviderConfig`; model binds **per call-site** (the 5 aux helpers keep their model-override envs as selectors within the answerer provider, never `base_url` switches).
- FR-M3: Two first-class **tested** targets — `openai` and `azure`. `openai` also accepts an optional `base_url` for any OpenAI-compatible endpoint (supported-but-untested). Azure handles the deployment-vs-model split, `/openai/deployments/{deployment}/chat/completions` path-templating, the `api-version` query param, and **api-key auth only**.
- FR-M4: **Responses mode is OpenAI-provider-only and validated fail-closed** at startup (answerer≠openai AND chat-mode=responses → reject, never silent fallback). The cross-provider default is `completions`.
- FR-M5: The embedder is guarded **fail-closed** by an `embedding_config(model,dim,indexed_at)` stamp + a startup probe-embed that measures the actual returned length and refuses to start unless it matches **both** the column dim and the stamped model; the error carries the re-index remedy. Re-embedding preserves chunk UUIDs (and thus `chunk_acl` grants).
- FR-M6: The runtime judge role is bound by the Chat Completions contract; the offline cross-family Claude judge is **owned by the eval harness**, not this surface, and is excluded from every model-surface guard/validation.

#### Non-goals (this area)
- Native non-OpenAI **runtime** APIs (Anthropic / Bedrock native SDKs) — a non-OpenAI model reaches the surface only via an OpenAI-compatible endpoint.
- Entra / AAD-token auth for Azure (api-key auth only in v1; documented-but-deferred).
- Swapping the **offline** cross-family Claude judge per deployment — it stays native `AsyncAnthropic`, fixed measurement instrument, owned by the eval harness (ADR-0005), not configurable through this surface.
- Per-call-site **provider / `base_url`** granularity — provider granularity is role; a deployment uses one chat host for all text generation.
- Layout-aware / structured model I/O (richer-than-markdown parser output) — an Ingestion-boundary concern, not the model surface.
- Folding the Cohere/Voyage rerankers into the model-surface roles — they are a separate provider axis (dedicated rerank endpoints), orthogonal to answerer/embedder/judge.
## C. Ingestion parser boundary (ADR-0007)

docling already sits behind a clean implicit seam: it lives only in `backend/parsing.py`, the single entry `parse_document(raw, filename, content_type) -> str` returns normalized markdown, and nothing docling-typed escapes (`backend/chunking.py` consumes a markdown string with zero docling awareness). So I5 is a *verify-and-formalize*, not a refactor — there is no threading to undo. This area (1) proves the seam is clean, (2) lifts the implicit function seam into a one-method `DocumentParser` protocol + factory mirroring the existing `Reranker` / `WebSearchProvider` pattern (`PARSER=docling|unstructured|llamaparse`, contract unchanged: bytes + filename + content_type → markdown str), (3) wires `PARSER` env selection at the call site, and (4) ships **one real working commercial adapter — LlamaParse** — smoke-tested behind the protocol, plus a "write your own adapter" guide. The output contract is a normalized markdown **string** (v1, pinned): a parser emitting structured output must flatten to markdown at the boundary; widening the type for layout-aware chunking is a documented future change.

### US-036: Verify the docling seam is clean (I5)
**Description:** As a kit maintainer, I want a verification that nothing docling-typed escapes `parsing.py` and that the chunker consumes only `str`, so that I can close I5 as "verified" with evidence rather than assertion.
**Acceptance Criteria:**
- [ ] A test/check proves `docling` is imported in exactly one module — `backend/parsing.py` (the entry `parse_document` at `backend/parsing.py:112`); no other `backend/*.py` imports `docling`, `DocumentConverter`, `InputFormat`, or `DocumentStream`.
- [ ] A check proves `backend/chunking.py` accepts and operates on `str` only — `chunk_text(text: str, ...)` (`backend/chunking.py:109`) has no `docling`/`InputFormat`/`DocumentStream` reference and no non-`str` document type in its signature.
- [ ] The verified state is documented inline (CONTEXT "Ingestion boundary (I5 / I6)" + ADR-0007), stating I5 = verified, the PRD "PARTIAL/refactor if threaded" is moot.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Clean checkout; `cd backend`.
- **Steps:**
  1. `grep -rEl "import docling|from docling|DocumentConverter|InputFormat|DocumentStream" backend --include="*.py" | grep -v "\.venv" | grep -v "__pycache__"` and confirm the only path printed is `backend/parsing.py`.
  2. `grep -nE "docling|InputFormat|DocumentStream" backend/chunking.py` and confirm zero matches.
  3. Inspect `chunk_text`'s signature (`backend/chunking.py:109`) and confirm the document argument is typed `str`.
- **Expected Result:** Step 1 prints only `backend/parsing.py`; steps 2 prints nothing; step 3 shows `text: str`. Seam is confirmed single-module with a `str` output contract.
- **Failure Indicator:** Any `backend/*.py` other than `parsing.py` imports a docling symbol, or `chunking.py` references a docling type, or `chunk_text` takes a non-`str` document — meaning docling has leaked past the seam and I5 is *not* clean.

### US-037: Define the `DocumentParser` protocol (boundary contract)
**Description:** As a kit maintainer, I want a one-method `DocumentParser` ABC modeled on `Reranker` (`backend/reranking.py:69`) / `WebSearchProvider` (`backend/web_search.py:85`), so that the implicit function seam becomes a discoverable, typed extension point consistent with the other two boundaries.
**Acceptance Criteria:**
- [ ] A `DocumentParser` ABC exists in `backend/parsing.py` (or a sibling it imports) with `name: str` and a single abstractmethod `parse(self, raw: bytes, filename: str, content_type: str | None) -> str`, matching today's `parse_document` signature exactly (`backend/parsing.py:112`).
- [ ] The docstring pins the contract: input is bytes + filename + content_type; output is **normalized markdown string** (v1); a parser with native structured output MUST flatten to markdown here (the chunker is markdown/heading-aware only).
- [ ] The ABC shape mirrors the existing pattern (class-level `name`, one abstractmethod), so a reader who knows `Reranker` recognizes it immediately.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `cd backend`.
- **Steps:**
  1. Import `DocumentParser`; assert it is `abc.ABC` and that instantiating it directly raises `TypeError` (abstractmethod unimplemented).
  2. Confirm the `parse` signature is `(bytes, str, str | None) -> str` via `inspect.signature`.
- **Expected Result:** Direct instantiation raises `TypeError`; signature matches the pinned contract and `parse_document`'s current parameters.
- **Failure Indicator:** The ABC is instantiable, the method is not abstract, or the signature diverges from `bytes + filename + content_type -> str`.

### US-038: Wrap docling as the default `DoclingParser` adapter
**Description:** As a kit maintainer, I want today's docling logic (including the `pypdfium2` PDF fallback) wrapped behind the protocol as `DoclingParser`, so that the default path is the first concrete `DocumentParser` and behavior is unchanged.
**Acceptance Criteria:**
- [ ] `DoclingParser(DocumentParser)` with `name = "docling"` exists; its `parse(...)` delegates to the existing docling conversion + `_pdf_text_fallback` logic (`backend/parsing.py:141-163`) and returns identical output for the same input.
- [ ] The `pypdfium2` PDF fallback stays **inside** the docling adapter (a docling-path concern per ADR-0007), not promoted to the boundary.
- [ ] `parse_document(...)` remains available as a thin wrapper (back-compat for `main.py:1348`) that delegates to the selected parser, OR the call site is migrated in US-039 — no behavior change either way.
- [ ] Existing US-018 multi-format behavior (`.pdf/.docx/.html/.md` → markdown, `.txt` verbatim, `UnsupportedFormatError` on unknown) is preserved.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `cd backend`; use `test-fixtures/us018/` (`sample.pdf`, `sample.docx`, `sample.html`, `sample.md`, `sample.txt`).
- **Steps:**
  1. Build `DoclingParser()` and call `.parse(raw, filename, content_type)` for each fixture.
  2. Compare each output to calling the legacy `parse_document(raw, filename, content_type)` on the same bytes.
- **Expected Result:** Each `.parse()` returns a non-empty markdown string; outputs are byte-identical to the legacy `parse_document` results; `.txt` is returned verbatim; an unknown extension still raises `UnsupportedFormatError`.
- **Failure Indicator:** Output differs from legacy `parse_document`, the PDF fallback no longer triggers when docling's torch pipeline is unavailable, or markdown structure is lost.

### US-039: `PARSER` env selection + `build_parser` factory
**Description:** As an operator, I want `PARSER=docling|unstructured|llamaparse` to select the parser via a `build_parser` factory mirroring `build_reranker` (`backend/reranking.py:290`) / `build_web_search_provider` (`backend/web_search.py:222`), so that swapping the ingestion parser is a config switch (+ an API key for commercial adapters).
**Acceptance Criteria:**
- [ ] `get_parser_name()` reads `PARSER` (default `docling`), validates against `docling|unstructured|llamaparse`, and raises a clear `ValueError` on an unknown value — same shape as `get_reranker_name()` (`backend/reranking.py:45`).
- [ ] `build_parser(name)` returns the concrete `DocumentParser`; commercial adapters raise at **build time** on a missing API key (e.g. `PARSER=llamaparse requires LLAMA_CLOUD_API_KEY`), matching the build-time key check in `build_reranker` / `build_web_search_provider`.
- [ ] The ingest path (`backend/main.py:1348`) routes through the selected parser instead of calling the docling-only function directly; default `PARSER=docling` preserves today's behavior exactly.
- [ ] `unstructured` is a **named, accepted value** but may be a documented "bring your own adapter" slot (not required to ship a working impl in v1 — only LlamaParse is the shipped commercial adapter); selecting it without an implementation must fail loudly, never silently fall back to docling.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `cd backend`.
- **Steps:**
  1. With `PARSER` unset, `build_parser(get_parser_name())` returns a `DoclingParser` (`name == "docling"`).
  2. Set `PARSER=bogus` → `get_parser_name()` raises `ValueError` listing the valid options.
  3. Set `PARSER=llamaparse` with `LLAMA_CLOUD_API_KEY` unset → `build_parser` raises `ValueError` naming the missing key (no network call).
- **Expected Result:** Default resolves to docling; invalid value fails fast; missing commercial key fails at build time with an actionable message.
- **Failure Indicator:** An unknown `PARSER` value silently defaults to docling, the missing-key error only surfaces on first ingest (not at build), or `unstructured` silently falls back to docling.

### US-040: LlamaParse adapter implementation (I6)
**Description:** As a buyer with scanned / complex-layout / OCR documents (PRD §3.3 out-of-scope for the default docling parser), I want a real working `LlamaParseParser` behind the protocol, so that the OCR escape hatch is a config switch + API key, not a stub.
**Acceptance Criteria:**
- [ ] `LlamaParseParser(DocumentParser)` with `name = "llamaparse"` calls the LlamaParse cloud API (`LLAMA_CLOUD_API_KEY`), requesting **markdown** result type, and returns the markdown string — flattening any structured output (tables/layout) to markdown **at the boundary** per the v1 contract.
- [ ] Constructor injects the HTTP client / key (testable seam), matching how `CohereReranker` / `TavilyProvider` take `http` + `api_key` (`backend/reranking.py:112`, `backend/web_search.py:118`).
- [ ] Chosen for the simplest API + strongest OCR/complex-layout coverage — the gap docling cannot cover; documented as the reason LlamaParse (not Unstructured) is the shipped example (ADR-0007 alternatives).
- [ ] The LlamaParse SDK/dep (or a documented raw-HTTP call) is added to `backend/requirements.txt` with a comment tying it to I6, consistent with the existing dep-comment convention.
- [ ] Errors surface as `ValueError`/HTTP errors that the ingest path turns into `documents.error_message`, consistent with the docling path (`backend/main.py:1342-1354`).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `cd backend`; inject a stub HTTP transport returning a canned LlamaParse markdown response (no live key needed for the unit-level test).
- **Steps:**
  1. Construct `LlamaParseParser(http=<stub>, api_key="test")`.
  2. Call `.parse(raw=<pdf bytes>, filename="sample.pdf", content_type="application/pdf")`.
  3. Assert the returned value is a markdown `str` derived from the stubbed response (e.g. headings/tables flattened to `#`/markdown table syntax).
- **Expected Result:** `.parse()` returns a markdown string matching the `DocumentParser` contract; structured fields in the stub response are flattened to markdown, not surfaced as a non-`str` type.
- **Failure Indicator:** `.parse()` returns non-markdown (raw JSON / structured object), leaks a LlamaParse-typed object past the boundary, or hard-requires a live network call to construct.

### US-041: LlamaParse real round-trip smoke test (I6)
**Description:** As a kit maintainer, I want a smoke test that runs a **real** LlamaParse round-trip on a fixture when a key is present, so that "the swap works" is proven, not asserted — a stub would not prove I6.
**Acceptance Criteria:**
- [ ] A smoke test sets `PARSER=llamaparse`, builds the parser via `build_parser`, and parses a real fixture (`test-fixtures/us018/sample.pdf`) through to a non-empty markdown string.
- [ ] The smoke test runs against the live LlamaParse API when `LLAMA_CLOUD_API_KEY` is set, and **skips** (not fails) when the key is absent — so CI without the key stays green while a keyed run proves the round-trip (mirrors the opt-in-by-key posture of the reranker/web-search suites).
- [ ] The test asserts the result is markdown (`str`, non-empty, contains recognizable content from the fixture), proving end-to-end: `PARSER` env → factory → adapter → live API → markdown → ready for `chunk_text`.
- [ ] Documented one-liner to run it locally with a key.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Export a valid `LLAMA_CLOUD_API_KEY`; `cd backend`; `PARSER=llamaparse`.
- **Steps:**
  1. Run the smoke test against `test-fixtures/us018/sample.pdf`.
  2. Re-run with `LLAMA_CLOUD_API_KEY` unset.
- **Expected Result:** With the key, the test passes and produces non-empty markdown extracted from the fixture by the live API; without the key, the test is reported **skipped**, not failed.
- **Failure Indicator:** The keyed run returns empty/non-markdown or errors; or the keyless run **fails** (rather than skipping), breaking CI for contributors without a LlamaParse account.

### US-042: Feed selected-parser output into the chunker unchanged (contract integration)
**Description:** As a kit maintainer, I want any adapter's markdown output to flow into `chunk_text` (`backend/chunking.py:109`) with no parser-specific handling, so that the markdown-string contract is what actually couples the boundary to the chunker.
**Acceptance Criteria:**
- [ ] Ingestion calls `chunk_text(parser.parse(...))` with no `isinstance`/`name ==` branch on the parser between parse and chunk — the chunker stays parser-agnostic.
- [ ] A test parses one fixture through both `DoclingParser` and `LlamaParseParser` (stubbed) and confirms both outputs are accepted by `chunk_text` and yield non-empty chunk lists.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** `cd backend`; stub the LlamaParse transport.
- **Steps:**
  1. `chunks_a = chunk_text(DoclingParser().parse(raw, "sample.md", "text/markdown"))`.
  2. `chunks_b = chunk_text(LlamaParseParser(http=<stub>, api_key="test").parse(raw, "sample.pdf", "application/pdf"))`.
  3. Assert both are non-empty `list[str]`.
- **Expected Result:** Both parsers produce markdown that `chunk_text` consumes identically; no parser-specific code path exists between parse and chunk.
- **Failure Indicator:** The chunker needs to know which parser produced the text, or one adapter's output is rejected by `chunk_text`.

### US-043: "Write your own adapter" authoring guide
**Description:** As a buyer who needs a parser other than docling/LlamaParse (e.g. Unstructured.io, a homegrown OCR pipeline), I want a documented guide to author a `DocumentParser`, so that adding a parser is a known, supported path — not reverse-engineering.
**Acceptance Criteria:**
- [ ] A doc (e.g. `docs/` or backend README section) shows the minimal `DocumentParser` subclass (set `name`, implement `parse(...) -> str`), how to register it in `build_parser`, and how to select it via `PARSER`.
- [ ] The guide states the **load-bearing rule**: the parser MUST return a normalized markdown **string**; structured output (tables/bboxes/layout) MUST be flattened to markdown here because the chunker is markdown/heading-aware only.
- [ ] The guide names **Unstructured.io** as the canonical buyer-written example behind the same protocol (the rejected-as-default-but-valid alternative from ADR-0007), and links the LlamaParse adapter as the worked reference.
- [ ] The guide points at the LlamaParse smoke test (US-041) as the template for proving a new adapter's round-trip.
- [ ] The guide notes the future widening (structured output → layout-aware chunker) is explicitly out of v1, so an author should not try to return non-`str`.
**Validation Test:**
- **Setup:** Follow the guide from scratch to author a trivial `EchoParser` returning `raw.decode()`.
- **Steps:**
  1. Implement the subclass per the guide, register it in `build_parser`, add its name to the allowed `PARSER` values.
  2. Set `PARSER=<echo>` and parse `test-fixtures/foo.txt`.
- **Expected Result:** Following only the guide, a developer registers and selects a new parser and ingests a document end-to-end; the guide's steps are sufficient and accurate.
- **Failure Indicator:** A step is missing/wrong (e.g. the registration point or the `PARSER` validation list isn't mentioned), or the guide implies a parser may return non-markdown/non-`str`.

### US-044: F3 capability-matrix rows for the ingestion boundary
**Description:** As a buyer evaluating ingestion fidelity, I want the accepted gaps recorded in the F3 capability matrix, so that the markdown-string ceiling and OCR posture are honestly disclosed, per the "every accepted gap gets an F3 row" discipline.
**Acceptance Criteria:**
- [ ] F3 row: default parser is docling (text-based PDF/DOCX/HTML/MD); **OCR/scanned/image-only is out-of-scope for the default** (PRD §3.3) — covered only by selecting the LlamaParse adapter.
- [ ] F3 row: the boundary output contract is a **markdown string (v1)** — table structure / layout fidelity beyond what markdown preserves is a documented **future widening**, not a v1 feature.
- [ ] F3 row: `PARSER=unstructured` is a buyer-written adapter slot, not a shipped working adapter (LlamaParse is the one shipped commercial example).
- [ ] Rows cite ADR-0007.
**Validation Test:**
- **Setup:** Open the F3 matrix.
- **Steps:** 1. Locate the three ingestion rows. 2. Confirm each cites ADR-0007 and matches the decisions here.
- **Expected Result:** The matrix honestly states the OCR-default gap, the markdown-string ceiling, and the unstructured-is-BYO-adapter status.
- **Failure Indicator:** The matrix overstates ingestion (claims OCR/table fidelity by default), or omits the markdown-string ceiling.

### US-045: Document the future structured-output widening (non-goal boundary)
**Description:** As a kit maintainer, I want the layout-aware / structured-output future change captured as a documented non-goal seam, so that v1's markdown-string contract is a deliberate pin with a known upgrade path, not an oversight.
**Acceptance Criteria:**
- [ ] ADR-0007 (already) + CONTEXT record that widening the boundary's output type to carry structure (tables/bboxes/layout) for a layout-aware chunker is a **future** change that touches `chunking.py`, explicitly not built in v1.
- [ ] The "write your own adapter" guide (US-043) references this so an author understands why `parse` returns `str` today.
- [ ] No v1 code carries a non-`str` document type across the boundary (consistent with US-036's verification).
**Validation Test:**
- **Setup:** Read ADR-0007 + CONTEXT "Ingestion boundary" + the authoring guide.
- **Steps:** 1. Confirm each states the markdown-string pin and the future widening. 2. Re-run US-036's grep to confirm no non-`str` document type crosses the seam in v1.
- **Expected Result:** The pin and its future upgrade path are documented in all three places; v1 code holds the `str` contract.
- **Failure Indicator:** The widening is undocumented (looks like a limitation, not a decision), or v1 code already leaks a structured type across the boundary.

#### Functional requirements (this area)
- FR-I1: docling is imported in exactly one backend module (`backend/parsing.py`); the chunker (`backend/chunking.py`) consumes only a `str` — verifiable by grep/import check (I5, verified not refactored).
- FR-I2: A one-method `DocumentParser` ABC defines the boundary: `parse(raw: bytes, filename: str, content_type: str | None) -> str`, output normalized markdown string (v1, pinned), mirroring `Reranker` / `WebSearchProvider`.
- FR-I3: `build_parser` + `PARSER=docling|unstructured|llamaparse` selects the parser; default `docling`; invalid value fails fast; commercial adapters raise at build time on a missing API key.
- FR-I4: `DoclingParser` is the default adapter; the `pypdfium2` PDF fallback stays inside it (not promoted to the boundary).
- FR-I5: A real working `LlamaParseParser` adapter ships behind the protocol (OCR/complex-layout escape hatch), with a real-round-trip smoke test that runs on a key and skips without one.
- FR-I6: Any adapter's markdown output feeds `chunk_text` with no parser-specific branch (the markdown-string contract is the only coupling).
- FR-I7: A "write your own adapter" guide documents subclass + register + select + the must-flatten-to-markdown rule, names Unstructured.io as the canonical BYO example, and points at the LlamaParse smoke test as the proof template.
- FR-I8: Accepted gaps (OCR-not-default, markdown-string ceiling, unstructured-is-BYO) are recorded as F3 rows citing ADR-0007.

#### Non-goals (this area)
- A layout-aware / structured (non-markdown) output contract — widening `parse` to carry tables/bboxes/layout for a layout-aware chunker is a **documented future** change touching `chunking.py`, not v1.
- OCR / scanned / image-only document support **in the default parser** — that is the LlamaParse adapter's job (PRD §3.3 out-of-scope for the default).
- Three documented stubs (Unstructured + LlamaParse + "yours") with no working adapter — exactly one *real* working commercial adapter ships (LlamaParse); a stub would not prove the swap.
- A shipped working `Unstructured.io` adapter — it remains a valid buyer-written adapter behind the same protocol, documented in the authoring guide, not built in v1.
- Multiple chunking strategies or any change to the heading-aware chunker — the chunker stays markdown/`str`-only; this area does not touch chunking behavior.
- Re-threading / refactoring docling out of the pipeline — there is nothing threaded to undo; I5 is verify-and-formalize only.
- Promoting the `pypdfium2` PDF fallback to the boundary — it is a docling-path concern that stays inside `DoclingParser`.
## D. Escalation signal & deterministic deflection pipeline (ADR-0003)

The support face answers a customer or escalates to a human via a **deterministic deflection pipeline** — `retrieve (hybrid, once) → retrieval gate → [if strong] draft → faithfulness gate → answer-or-escalate` — never the built agentic tool loop (M1). The escalation signal is retrieval-grounded **OR** faithfulness-grounded, run as a cascade that exploits the OR's short-circuit: the cheap cosine-defined retrieval gate fires first (escalate with no draft, no judge call), and only strong-retrieval queries pay for a draft plus one fast faithfulness judge call. The decision is control flow, never a model `escalate()` tool. E7 is a three-population golden set (P1a/P1b unanswerable, P2 auto-resolve, P3 unfaithful) whose runner sweeps the three knobs to the deflection-maximizing knee under a buyer-set false-resolve ceiling; the deterministic retrieval-gate tripwire runs per-PR while the LLM-judge sweep runs scheduled.

### US-046: Surface pre-fusion raw cosine on retrieval results
**Description:** As a platform engineer, I want `hybrid_search` to carry the pre-fusion raw cosine similarity on each result so that the retrieval gate can threshold "weak retrieval" on a calibrated `[0,1]` cosine instead of the RRF rank artifact.
**Acceptance Criteria:**
- [ ] `SearchDocumentsResult` in `backend/retrieval.py` gains a `cosine_similarity: float | None` field that carries the raw vector cosine from `match_chunks`; keyword-only rows (no embedding) carry `None` (`backend/retrieval.py`, around the `SearchDocumentsResult` model at line 137 and the `_rrf_fuse` copy at line 370).
- [ ] `_rrf_fuse` no longer loses the cosine: when it overwrites `similarity` with the summed RRF score (`model_copy(update={"similarity": ...})`), it preserves the vector side's `cosine_similarity` for each fused row (carried from the vector ranking's row, `None` when a chunk appeared only in the keyword ranking).
- [ ] `vector` and `keyword` modes are unaffected — `search_documents` sets `cosine_similarity` equal to its `similarity` (both are cosine), `keyword_only_search` sets it `None`; the eval runner's metric blocks are byte-stable.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Seed the e-commerce corpus; pick a query with both a strong vector hit and a strong keyword hit.
- **Steps:** 1. Call `hybrid_search` for the query. 2. Inspect the top fused row's `similarity` and `cosine_similarity`.
- **Expected Result:** `similarity` is the small RRF score (≈ ≤0.033 at k=60); `cosine_similarity` is a distinct value in `[0,1]` matching what `search_documents` returns for the same chunk. A fused row that appeared only in the keyword ranking has `cosine_similarity is None`.
- **Failure Indicator:** `cosine_similarity` equals the RRF score, is absent, or is `None` for a chunk that was a vector hit — the raw cosine was lost in fusion (the ADR-0003 plumbing bug).

### US-047: Cosine-defined retrieval gate (config knobs τ_sim, N)
**Description:** As a support-platform operator, I want a deterministic retrieval gate that judges retrieval "weak" from raw cosine so that a query with no good context escalates before any draft or judge call is made.
**Acceptance Criteria:**
- [ ] New net-new module (e.g. `backend/escalation.py`) exposes a pure function `retrieval_gate(results, tau_sim, n_min, match_threshold) -> RetrievalGateDecision` returning `{strong: bool, top1_cosine: float | None, n_cleared: int, reason}`.
- [ ] "Weak" is defined exactly per ADR-0003: `strong = (top1_cosine >= tau_sim) AND (n_cleared >= n_min)`, where `top1_cosine` is the max `cosine_similarity` across results (US-046) and `n_cleared` counts rows whose `cosine_similarity >= match_threshold`. Empty results ⇒ weak.
- [ ] The gate reads only `cosine_similarity` (never `similarity`/RRF) and is **pure arithmetic on scores** — no LLM, no reranker dependency. It survives deletion of the optional reranker module (R4); a reranker, when present, may supply a calibrated score in place of cosine but the gate contract stays cosine.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Construct three `SearchDocumentsResult` lists: (a) top1 cosine 0.62 with 4 rows ≥ threshold; (b) top1 cosine 0.18; (c) empty list. Use `tau_sim=0.4`, `n_min=2`, `match_threshold=0.3`.
- **Steps:** 1. Call `retrieval_gate` on each. 2. Read `.strong`.
- **Expected Result:** (a) `strong=True`; (b) `strong=False` (top1 below τ_sim); (c) `strong=False` (empty). Identical inputs always yield identical decisions.
- **Failure Indicator:** Any non-determinism, or the gate thresholding `similarity` (RRF) so a high-RRF/low-cosine query is wrongly called strong.

### US-048: One-call runtime faithfulness gate (net-new, latency path)
**Description:** As a support-platform operator, I want a single structured-output judge call to verify a drafted answer is supported by its retrieved chunks so that a confidently-wrong answer is caught before it auto-sends.
**Acceptance Criteria:**
- [ ] Net-new `faithfulness_gate(draft, chunks, cutoff) -> FaithfulnessDecision` in the request latency path makes **exactly one** structured-output call returning `{supported: bool, score: float}` on a cheap/fast judge model (the ADR-0006 runtime judge role; `gpt-4o-mini` / `haiku`-class) — **not** full RAGAS claim-decomposition.
- [ ] The decision is `supported AND score >= cutoff`; a judge error / parse failure / timeout fails **closed** (treated as unfaithful ⇒ escalate), never open.
- [ ] This code is explicitly distinct from the offline RAGAS faithfulness metric in `evals/retrieval/ragas.py` (which makes several calls, runs weekly); a code comment states "runtime gate, net-new — not the offline RAGAS metric" so the same word "faithfulness" never conflates the two machineries.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A draft fully grounded in two chunks, and a draft asserting a fact absent from its chunks. `cutoff=0.7`.
- **Steps:** 1. Run `faithfulness_gate` on each. 2. Count judge calls made. 3. Force a judge exception on a third call.
- **Expected Result:** Grounded draft → `supported=True`, passes; ungrounded draft → fails; exactly one judge call per evaluation; the forced-error case returns "unfaithful" (escalate).
- **Failure Indicator:** More than one judge call, a RAGAS-style multi-call decomposition, or a judge error defaulting to "faithful" (fail-open) and auto-sending.

### US-049: Deterministic deflection pipeline orchestrator
**Description:** As a customer using the support widget, I want my message answered or escalated by a deterministic pipeline so that the cheap retrieval gate short-circuits weak queries with no wasted draft and the model never decides on its own to resolve.
**Acceptance Criteria:**
- [ ] An orchestrator runs the exact ADR-0003 control flow: `retrieve (hybrid, once) → retrieval gate → [if strong] draft → faithfulness gate → answer-or-escalate`. It does **not** call the M1 agentic loop (`for _iteration in range(MAX_TOOL_ITERATIONS)` at `backend/main.py:941`); the shared machinery is retrieval + the gates ("one engine"), not the loop.
- [ ] Short-circuit proven: when the retrieval gate returns weak, the orchestrator escalates having made **zero** draft-generation and **zero** faithfulness-judge calls (the OR's left operand decided it).
- [ ] On escalate, the customer-facing output is the generic deferral ("a human will follow up") with **no** `reason`/access metadata; on answer, the drafted-and-faithful answer is returned. No `escalate()` tool is registered for the model — the answer-vs-escalate decision is in deterministic control flow only.
- [ ] User-initiated "talk to a human" is out of scope here (a separate widget button owned by the support-surface section); reference it, do not implement it.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Two messages — one with strong corpus support, one off-topic with no support — against a seeded support bot. Instrument draft + judge call counts.
- **Steps:** 1. Run the off-topic message through the orchestrator. 2. Run the supported message. 3. Inspect call counts and outputs.
- **Expected Result:** Off-topic → escalate, generic deferral, 0 draft calls, 0 judge calls. Supported → drafted answer that passed the faithfulness gate (1 draft, 1 judge call). The agentic loop is never entered.
- **Failure Indicator:** A weak-retrieval query produces a draft or a judge call (short-circuit destroyed); the escalation output leaks a `reason`; or the pipeline routes through `MAX_TOOL_ITERATIONS`.

### US-050: Escalation config — three global knobs + false-resolve ceiling
**Description:** As a buyer, I want τ_sim, N, the faithfulness cutoff, and a false-resolve ceiling expressed as global config so that I set my risk tolerance with one number and the gate defaults come from the E7 sweep.
**Acceptance Criteria:**
- [ ] Three gate knobs (`ESCALATION_TAU_SIM`, `ESCALATION_N_MIN`, `ESCALATION_FAITHFULNESS_CUTOFF` or equivalent) are read as typed, validated global config with defaults sourced from the E7 sweep knee (US-058); validation rejects out-of-range values (`τ_sim`/cutoff ∈ `[0,1]`, `N ≥ 1`), mirroring `get_similarity_threshold` in `backend/retrieval.py`.
- [ ] The **false-resolve ceiling** is a separate config value documented as "the one number a buyer sets as their risk tolerance"; it is consumed by the E7 sweep / knee selection (US-058) and the E8 gate (US-059), not by the per-request pipeline.
- [ ] Per-workspace tuning is explicitly deferred but **config-shaped** — a comment notes a future per-workspace override key resolved on top of the global default, no schema migration implied.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Set `ESCALATION_TAU_SIM=0.4`, `ESCALATION_N_MIN=2`, cutoff `0.7`; then set `τ_sim=1.5`.
- **Steps:** 1. Load config with valid values. 2. Load with `τ_sim=1.5`. 3. Confirm the pipeline reads these defaults.
- **Expected Result:** Valid config loads and the gate uses those values; `τ_sim=1.5` raises a clear validation error at startup; omitting all knobs yields the documented E7-derived defaults.
- **Failure Indicator:** Out-of-range knobs silently accepted, a knob hardcoded in the gate instead of config-driven, or the false-resolve ceiling wired into the per-request path.

### US-051: E7 golden-set schema + escalation labels (derived from gold)
**Description:** As an eval author, I want each golden question to carry one escalation label so that the E7 populations are derivable from the existing gold-chunk labels rather than hand-authored as a separate set.
**Acceptance Criteria:**
- [ ] The escalation golden set carries one `escalation` label per question: `no_context` (P1a) / `answerable_faithful` (P2) / `should_escalate` (P3), per the E9 support-face layer; the loader validates the label is one of these three (mirroring the `category` enum check in `evals/retrieval/runner.py::load_questions`).
- [ ] P1a rows are genuinely-no-context questions (no gold chunks in the corpus); P2 and P3 rows reuse the existing `gold_stable_ids` content-anchor mechanism. P2-vs-P3 is the only human judgment ("does a faithful answer exist from these chunks?").
- [ ] P1b is **not** a hand-authored row — it is the derived viewer-parameterized case (US-057), reusing the runner's `compute_visible_stable_ids` no-access construction. The schema does not add a `P1b` label.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** An escalation golden YAML with one P1a, one P2, one P3 row, plus one row with an invalid `escalation` value.
- **Steps:** 1. Load the valid rows. 2. Load the invalid row.
- **Expected Result:** Valid rows load with their labels; the invalid label raises a clear loader error naming the allowed enum.
- **Failure Indicator:** An out-of-enum label silently accepted, or a hand-written P1b row appearing in the set.

### US-052: E7 population P1a — genuinely-no-context escalation
**Description:** As an eval author, I want P1a questions scored against the retrieval gate so that I can prove genuinely-unanswerable queries escalate without a draft.
**Acceptance Criteria:**
- [ ] The E7 runner classifies a P1a row's outcome via the deterministic retrieval gate (US-047) reusing the real `backend` gate function — a future PR that breaks the gate breaks E7 (same "real backend functions" discipline as `evals/retrieval/runner.py`).
- [ ] P1a expected outcome is `escalate` with no draft/judge call; the runner records `top1_cosine` and `n_cleared` so a near-miss is visible in the JSON.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A P1a question whose answer is absent from the corpus.
- **Steps:** 1. Run E7 over the P1a row. 2. Inspect its recorded decision.
- **Expected Result:** Decision `escalate`, caught at the retrieval gate, 0 draft/judge calls.
- **Failure Indicator:** P1a row drafted or auto-resolved (a false-resolve), or scored through a non-deterministic path.

### US-053: E7 population P2 — answerable + faithful → auto-resolve
**Description:** As an eval author, I want P2 questions scored end-to-end so that I can measure the deflection rate on questions a faithful answer exists for.
**Acceptance Criteria:**
- [ ] P2 rows pass both gates: strong retrieval then a faithful draft; expected outcome `auto-resolve`. The faithfulness leg uses the offline RAGAS/cross-family infra (`evals/retrieval/ragas.py` + the Claude judge in `runner.py`) to **score** the gate, not the runtime one-call gate.
- [ ] A P2 row that escalates is counted as a **false-escalate** (annoyance cost), tallied per US-055.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A P2 question with strong gold support and a hand-authored reference answer.
- **Steps:** 1. Run E7 over the P2 row. 2. Inspect the decision and faithfulness score.
- **Expected Result:** Decision `auto-resolve`; counted toward deflection; faithfulness score recorded.
- **Failure Indicator:** A faithfully-answerable P2 escalating without being counted as a false-escalate, or being scored by the runtime gate instead of the offline judge.

### US-054: E7 population P3 — strong retrieval, no faithful answer → escalate
**Description:** As an eval author, I want P3 questions — strong retrieval but no faithful grounded answer — scored at the faithfulness gate so that the hardest, most important case (the moat) is proven.
**Acceptance Criteria:**
- [ ] P3 rows clear the retrieval gate (strong cosine) but the faithfulness leg must return unfaithful ⇒ expected outcome `escalate (faithfulness gate)`. The runner records both the gate decision and the offline faithfulness score.
- [ ] A P3 row that auto-resolves is a **false-resolve** (the Risk #3 safety failure) and is tallied toward the false-resolve rate (US-055), which the ceiling governs.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A P3 question whose retrieved chunks are topically strong but contain no grounded answer, with a reference marking it should-escalate.
- **Steps:** 1. Run E7 over the P3 row. 2. Inspect the decision.
- **Expected Result:** Strong retrieval recorded, faithfulness gate fails, decision `escalate`; not counted as a false-resolve.
- **Failure Indicator:** P3 auto-resolved (a false-resolve) and uncaught, or the row escalating at the retrieval gate (mislabeled — it must exercise the faithfulness gate).

### US-055: E7 metrics — deflection / false-resolve / false-escalate
**Description:** As an eval consumer, I want deflection rate, false-resolve rate, and false-escalate rate computed so that the operating objective (maximize deflection subject to false-resolve ≤ ceiling) is measurable.
**Acceptance Criteria:**
- [ ] The runner computes three rates: **deflection** = correctly auto-resolved / answerable (P2+P3 answerable subset, maximize); **false-resolve** = wrongly auto-resolved / unanswerable (the safety number); **false-escalate** = wrongly escalated / answerable (annoyance).
- [ ] Each rate carries its numerator/denominator and the population breakdown in the results JSON (so a regression is attributable to P1a/P2/P3).
- [ ] false-resolve is flagged as the safety metric whose ceiling is a pinned invariant (consumed by US-059); deflection/false-escalate are tunable quality metrics.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A small labeled set with a known mix of P1a/P2/P3 and a fixed knob setting producing one wrong auto-resolve and one wrong escalate.
- **Steps:** 1. Run the metrics computation. 2. Check the three rates against hand-computed values.
- **Expected Result:** Rates match the hand-computed numerator/denominator exactly; the false-resolve count includes the wrong auto-resolve.
- **Failure Indicator:** A false-resolve folded into a generic "accuracy" number, or rates that hide the denominator (cannot verify ≤ ceiling).

### US-056: E7 runner — knob sweep, deflection-vs-false-resolve curve, knee
**Description:** As an eval author, I want the runner to sweep τ_sim / N / faithfulness-cutoff and emit the deflection-vs-false-resolve curve so that the default operating point is the deflection-maximizing knee under the ceiling.
**Acceptance Criteria:**
- [ ] The runner sweeps a grid over the three knobs and, for each point, records (deflection, false-resolve, false-escalate); it emits the **deflection-vs-false-resolve curve** as structured data in the results JSON (and a summary-md table, like `render_summary` in `evals/retrieval/runner.py`).
- [ ] It selects the **knee** = the grid point maximizing deflection **subject to false-resolve ≤ ceiling**, and reports those knob values as the recommended config defaults (feeding US-050); if no point satisfies the ceiling it reports that explicitly rather than silently picking the least-bad.
- [ ] The sweep reuses the offline RAGAS/Claude faithfulness infra to score each point; it is structurally a scheduled (not per-PR) artifact because it is LLM-judged.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A labeled subset and a 2×2×2 knob grid with a known knee under a `false-resolve ≤ 0.05` ceiling.
- **Steps:** 1. Run the sweep. 2. Read the emitted curve and the chosen knee.
- **Expected Result:** The curve lists each grid point's (deflection, false-resolve); the knee is the highest-deflection point with false-resolve ≤ 0.05; knob values are reported.
- **Failure Indicator:** A knee with false-resolve above the ceiling chosen, no curve emitted, or "maximize accuracy" used instead of the ceiling-constrained objective.

### US-057: E7 P1b — viewer-parameterized no-access population (reuse E4)
**Description:** As an eval author, I want P1b generated by replaying P2 questions under a no-access viewer so that the access-filtered case is covered for free using the E4 viewer machinery, with no hand-authoring.
**Acceptance Criteria:**
- [ ] P1b rows are produced by running a P2 question under a no-access viewer via the existing `compute_visible_stable_ids(... "no_access" ...)` / `reset_viewer_acls` machinery in `evals/retrieval/runner.py` — the *same question* is P2 for a full-access viewer and P1b for a no-access viewer.
- [ ] From the no-access viewer's own retrieval, P1a and P1b are indistinguishable (filtered chunks invisible); the expected E7 decision for a P1b row is therefore the **same as P1a — escalate** (caught at the retrieval gate, since the viewer's retrieval is weak).
- [ ] **P1b detection (the privileged second pass) is absent from the code**, not merely default-OFF — there is no unfiltered-second-pass code path; P1b rows trivially expect P1a output (US-058 asserts the equality).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A P2 question with gold chunks, replayed under the no-access viewer (gold ACL-revoked).
- **Steps:** 1. Run the question as full-access → P2. 2. Run it as no-access → P1b. 3. Inspect both decisions.
- **Expected Result:** Full-access auto-resolves (P2); no-access escalates at the retrieval gate (P1b), with no privileged second pass invoked anywhere in the run.
- **Failure Indicator:** A privileged/unfiltered retrieval pass present in the code, or a P1b row resolving differently than P1a.

### US-058: E7 P1b non-disclosure assertion (pinned security invariant)
**Description:** As a security reviewer, I want an E7 assertion that the customer-facing P1b output is byte-for-byte identical to the P1a output so that escalating a no-access viewer never discloses that restricted content exists.
**Acceptance Criteria:**
- [ ] A pinned E7 assertion compares the **customer-facing output bytes** of every P1b row against the P1a generic deferral output and asserts exact equality (`assert p1b_customer_output == p1a_customer_output`, byte-for-byte) — no `reason`, no `restricted-to`, no existence bit echoed to the customer.
- [ ] The assertion is structured as a binary leak invariant (`assert leak == 0`), placed in the E8 **pinned `fail`** security/correctness class — not buyer-downgradable to `comment`/`off`; silencing it requires deleting the eval, not configuring it down.
- [ ] The assertion is **deterministic** (string/byte equality on the deferral output, no LLM judge), so it can hard-block per-PR (US-059); access-aware reason/routing surfacing is explicitly out of scope (a future S4 authorized-agent surface, gated on its own existence-non-disclosure eval).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** One P1a row and one P1b row (a P2 question under a no-access viewer) run through the customer-facing output path.
- **Steps:** 1. Capture the P1a customer output bytes. 2. Capture the P1b customer output bytes. 3. Run the assertion. 4. Inject a `reason=access-denied` into the P1b output and re-run.
- **Expected Result:** Bytes are identical and the assertion passes; the injected `reason` makes the assertion fail loudly (proving it actually pins the invariant).
- **Failure Indicator:** P1b output differing from P1a in any byte while the assertion still passes, or the assertion being configurable to `comment`/`off`.

### US-059: E7 CI placement — per-PR deterministic tripwire vs scheduled sweep
**Description:** As a maintainer, I want the deterministic retrieval-gate behavior checked per-PR and the LLM-judge sweep run scheduled so that a judge wobble never red-bars an innocent merge while a retrieval-gate regression is caught immediately.
**Acceptance Criteria:**
- [ ] A **per-PR tripwire** runs the deterministic retrieval-gate decisions (pure arithmetic on cosine scores — top-1 cosine `< τ_sim` / fewer than `N` cleared, no LLM) over a fixed labeled subset, plus the US-058 P1b non-disclosure byte-equality assertion; both may `fail` and block the merge (deterministic, like the retrieval recall floor in `.github/workflows/retrieval-eval.yml`).
- [ ] The **full E7 sweep with the LLM faithfulness judge** (P2/P3 scoring, the knob sweep + curve + knee) runs **scheduled/weekly** alongside the RAGAS weekly workflow (`.github/workflows/retrieval-eval-ragas-weekly.yml`, `cron`/`workflow_dispatch`); its `fail` = fail the scheduled workflow + file an issue, never block a merge.
- [ ] The configurable E8 gate governs comment-vs-fail on a non-invariant E7 regression (deflection/false-escalate); the false-resolve ceiling breach fails like a safety invariant; the P1b assertion stays pinned `fail`. The accepted gap — the faithfulness leg of false-resolve has up-to-a-week detection latency — is documented (F3/P5), mitigated by the per-PR retrieval-leg tripwire.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A PR that loosens the retrieval gate so a P1a row drafts (a retrieval-leg false-resolve), and separately a PR that only perturbs a faithfulness score.
- **Steps:** 1. Run the per-PR tripwire on the gate-loosening PR. 2. Run it on the faithfulness-only PR. 3. Confirm the weekly workflow runs the full sweep.
- **Expected Result:** The gate-loosening PR fails the per-PR tripwire (blocks merge); the faithfulness-only PR passes per-PR (caught later by the scheduled sweep); the weekly workflow runs the judge sweep + curve.
- **Failure Indicator:** An LLM judge call on the per-PR path (flaky merge gating), the P1b assertion only running weekly, or a per-PR `fail` offered for the judge-driven deflection metric.

#### Functional requirements (this area)
- FR-S1: The support face decides answer-vs-escalate via a deterministic deflection pipeline (`retrieve once → retrieval gate → [if strong] draft → faithfulness gate → answer-or-escalate`); the decision is control flow, never a model `escalate()` tool, and never the M1 agentic loop.
- FR-S2: The escalation signal is retrieval-grounded OR faithfulness-grounded, run as an OR-short-circuit cascade: the retrieval gate fires first (escalate with no draft, no judge call); only strong-retrieval queries draft + run the faithfulness gate.
- FR-S3: The retrieval gate is cosine-defined on raw `cosine_similarity ∈ [0,1]` (top-1 `< τ_sim` OR fewer than `N` clear `match_threshold`), never the RRF fused score; `retrieval.py` surfaces the pre-fusion cosine through fusion; the gate is reranker-optional.
- FR-S4: The runtime faithfulness gate is exactly one structured-output judge call `{supported, score}` on a cheap model, fail-closed, net-new code distinct from the offline RAGAS metric.
- FR-S5: τ_sim / N / faithfulness-cutoff are global typed config (defaults from the E7 knee); the false-resolve ceiling is the single buyer-set risk number; per-workspace tuning is deferred but config-shaped.
- FR-S6: E7 is a three-population golden set (P1a/P1b, P2, P3) scored by the offline RAGAS + cross-family judge infra; it computes deflection / false-resolve / false-escalate, sweeps the knobs, emits the deflection-vs-false-resolve curve, and picks the knee under the ceiling.
- FR-S7: Customer-facing P1b output is byte-for-byte identical to P1a (pinned, deterministic non-disclosure assertion); P1b detection / the privileged second pass is absent from v1 code.
- FR-S8: The deterministic retrieval-gate tripwire + the P1b non-disclosure assertion run per-PR and may block merge; the LLM-judge deflection/false-resolve sweep runs scheduled (weekly) and files an issue on regression rather than blocking.

#### Non-goals (this area)
- A standalone classifier escalation signal (PRD option 2) — cloneable, un-grounded; rejected in favor of the 3+4 retrieval+faithfulness cascade.
- A model-discretion `escalate()` tool — un-evalable, re-introduces Risk #3 overconfidence, cloneable; the decision stays deterministic control flow.
- Reusing the M1 agentic tool loop for the support face — the model has usually drafted by loop termination, destroying the OR short-circuit.
- Thresholding the RRF fused score for "weak retrieval" — RRF is a rank artifact, not comparable across queries.
- Full RAGAS claim-decomposition at runtime — several calls, fatal in the latency path; the runtime gate is one call.
- P1b detection / the privileged unfiltered second pass — absent from v1, not merely default-OFF; access-aware routing is a documented future capability gated on a content-blind-for-all design + its own existence-non-disclosure eval, never opened for the bot alone.
- Showing the customer a `reason=access-denied` / any existence bit on escalation — the access-aware reason is surfaced only to an authorized agent on the S4 handoff surface (out of scope here).
- Per-workspace knob tuning — deferred (config-shaped, no schema migration in v1).
- The user-initiated "talk to a human" widget button — owned by the support-surface section; referenced, not built here.
- A per-PR LLM-judged false-resolve check — the faithfulness leg is scheduled/weekly (accepted up-to-a-week detection latency); only the deterministic retrieval-leg tripwire blocks merge.
## E. Support surface — widget runtime + conversation/handoff (ADR-0008 + ADR-0004)

This section builds the anonymous embeddable support widget (S3) and the conversation-state + human-handoff model (S4) as one coherent runtime. It **consumes the workspace layer (ADR-0002)** — every new resource is gated by the *same* `viewer ∈ members(workspace_id)` membership clause and the active-workspace resolution rules (default-when-sole / 400-on-ambiguous), and the support bot is a `role='member'` `workspace_membership` principal — and it **consumes the escalation pipeline (ADR-0003)**: the per-turn answer-vs-escalate decision (retrieval gate → optional draft → faithfulness gate) is reused wholesale; this section only adds the *conversation-level latch* over it, the runtime that runs it for an anonymous customer, and the read-back channel that returns a human reply. The widget is a cross-origin iframe served from the kit's own origin plus a tiny loader `<script>` (max-isolation embed); the backend self-signs short-lived Supabase-compatible JWTs (one minting primitive) for the **bot** (server-side only) while the **customer** holds only an opaque per-conversation token and is structurally off the Supabase JWT/Realtime trust surface. S5 teams/assignment routing and P1b access-aware detection are explicitly out of scope. Stories below assume the ADR-0002 workspace section landed `workspace_membership(user_id, workspace_id, role)` and the membership clause inside `match_chunks` *before* this section's migrations run.

### US-066: `conversations` + `conversation_messages` migration with membership RLS
**Description:** As the platform, I want a new `conversations` + `conversation_messages` table pair (NOT an extension of `threads`) so that support conversations have workspace-membership RLS without branching the leak-proof `threads`/`messages` predicate on a `kind` discriminator (ADR-0004, PRD Risk #3).
**Acceptance Criteria:**
- [ ] New migration creates `conversations(id uuid pk, workspace_id uuid NOT NULL references workspaces, bot_user_id uuid references auth.users, customer_email text NULL, status text NOT NULL default 'active', escalated_at timestamptz NULL, channel text NOT NULL default 'widget', claimed_by uuid NULL references auth.users, claimed_at timestamptz NULL, created_at timestamptz NOT NULL default now())` per ADR-0004 + CONTEXT (claimed_by/claimed_at added by ADR-0008).
- [ ] `conversation_messages` mirrors `messages` shape: `(id, conversation_id references conversations on delete cascade, role check in ('user','assistant','system','tool'), content text, tool_calls jsonb NULL, tool_call_id text NULL, name text NULL, created_at)` — `tool_calls` kept for schema parity, null/unused for widget convos.
- [ ] RLS enabled on both; `conversations` SELECT/UPDATE policy = `EXISTS (select 1 from workspace_membership wm where wm.workspace_id = conversations.workspace_id and wm.user_id = auth.uid())` — the SAME ADR-0002 clause; `role` appears in NO predicate. `conversation_messages` policies delegate to the parent conversation's membership (mirrors `messages`→`threads` in `20260416120000_init_threads_messages.sql`).
- [ ] `threads`/`messages` tables and their owner-only policies are untouched.
- [ ] Index on `conversations(workspace_id, status)` (queue lists escalated per workspace) and `conversation_messages(conversation_id, created_at asc)`.
- [ ] Typecheck/lint passes and migration applies.
**Validation Test:**
- **Setup:** Apply migration. Seed two workspaces W1, W2; user U1 ∈ W1 only. Insert a conversation C1 in W1, C2 in W2.
- **Steps:** 1. As U1 (real JWT) `GET /rest/v1/conversations`. 2. Attempt to read C2 directly by id.
- **Expected Result:** C1 returned; C2 returns 0 rows (RLS-hidden, indistinguishable from not-found).
- **Failure Indicator:** U1 can read C2, or any `conversations` policy references `wm.role`.

### US-067: Conversation status machine + escalation latch + derivable deflection
**Description:** As the platform, I want `active → escalated → resolved` enforced as a one-way latch with `escalated_at` set exactly once so that deflection is derivable (`resolved AND escalated_at IS NULL` = deflected; `IS NOT NULL` = human-handled) without a `resolved_by_bot`/`resolved_by_human` state explosion (ADR-0004).
**Acceptance Criteria:**
- [ ] DB CHECK constraint `status in ('active','escalated','resolved')`; a transition guard (trigger or service-layer assertion) rejects `escalated → active` and `resolved → *` (resolved is terminal).
- [ ] `escalated_at` is set on the *first* escalate transition and is never cleared on later writes (latch); set-once enforced (trigger refuses to overwrite a non-null `escalated_at`).
- [ ] First escalating turn latches the whole conversation: after latch the bot never runs the ADR-0003 pipeline again for that conversation (asserted in US-080).
- [ ] A documented SQL snippet computes deflection rate from production data: `count(*) filter (where status='resolved' and escalated_at is null) / count(*) filter (where status='resolved')`.
- [ ] Typecheck/lint passes and migration applies.
**Validation Test:**
- **Setup:** Conversation C in `active`.
- **Steps:** 1. Transition to `escalated`, record `escalated_at = t0`. 2. Attempt a second escalate write (should not move `escalated_at`). 3. Attempt `escalated → active`. 4. Transition to `resolved`; attempt `resolved → escalated`.
- **Expected Result:** `escalated_at` stays `t0`; step 3 rejected; step 4 second transition rejected (terminal).
- **Failure Indicator:** `escalated_at` changes after first set, or any backward/out-of-terminal transition succeeds.

### US-068: Self-signed Supabase-compatible JWT minting primitive (server-side)
**Description:** As the backend, I want one minting primitive that self-signs short-lived HS256 Supabase-compatible JWTs with the **project JWT secret** so that the bot's `sub=bot_user_id` token is an ordinary `role=authenticated` JWT that `auth.uid()` + RLS resolve natively — a new *issuer* beside GoTrue, no new enforcement path (ADR-0008).
**Acceptance Criteria:**
- [ ] A `mint_supabase_jwt(sub, ttl_seconds)` helper signs HS256 with `SUPABASE_JWT_SECRET` (NEW env, documented as a P5 threat-model line — backend now holds the signing secret, previously only the anon key); claims include `sub`, `role='authenticated'`, `aud='authenticated'`, `exp`, `iat`.
- [ ] Used ONLY server-side for the bot token (~60s); the function is never exposed over any endpoint and its output never reaches an HTTP response body sent to the iframe.
- [ ] A token minted by this helper is accepted by PostgREST/`match_chunks` exactly like a GoTrue-issued JWT (resolves `auth.uid()` to `sub`).
- [ ] Chosen over a GoTrue admin-API session per request (avoids round-trip + service-role key in the request path) — recorded in code comment referencing ADR-0008.
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Configure `SUPABASE_JWT_SECRET` to the project secret. Have a known `auth.users` id `B`.
- **Steps:** 1. `mint_supabase_jwt(sub=B, ttl=60)`. 2. Call `GET /rest/v1/...` or RPC `match_chunks` with that token. 3. Wait past TTL, retry.
- **Expected Result:** Step 2 resolves `auth.uid() == B`; step 3 rejected (expired).
- **Failure Indicator:** A minted token is rejected by PostgREST when fresh, or accepted after `exp`, or appears in any client-facing payload.

### US-069: Lazy support-bot provisioning + `is_bot` flag (one bot per workspace)
**Description:** As an admin enabling support, I want the per-workspace support bot (`auth.users` row + `workspace_membership` with `role='member'`, `is_bot=true`) created lazily on first widget-key issuance so that a knowledge-assistant-only deployment never spawns a bot and no new content role is added (ADR-0008; ADR-0002 intact).
**Acceptance Criteria:**
- [ ] Migration adds `is_bot boolean NOT NULL default false` to `workspace_membership` (a flag, not a role — changes no retrieval predicate; ADR-0002 untouched).
- [ ] Provisioning (service-role/admin API) creates the bot `auth.users` row + a `workspace_membership(role='member', is_bot=true)` row, runs **lazily** when support is first enabled (first key issued), idempotently — exactly **one bot per workspace**, not per key; `conversations.bot_user_id` is set from it.
- [ ] Bot membership is excluded from member-management UI listings and is available for an optional explicit write-deny policy.
- [ ] Bot is `role='member'` — administrative-only role grants no content access, so the bot sees only documents granted via share-to-bot through `chunk_acl`; NO dedicated `bot` content-role exists anywhere.
- [ ] Typecheck/lint passes and migration applies.
**Validation Test:**
- **Setup:** Workspace W with no support enabled (no bot row).
- **Steps:** 1. Issue first widget key for W (US-072). 2. Issue a second key for W. 3. Inspect `workspace_membership` for W.
- **Expected Result:** Exactly one `is_bot=true` member row exists after both keys; `role='member'`; second key issuance does NOT create a second bot.
- **Failure Indicator:** Two bot rows, a bot with `role='admin'`, or a bot created at workspace creation before any key.

### US-070: Support-bot retrieval — per-turn bot token calls `match_chunks` as the bot
**Description:** As the deflection pipeline, I want each customer turn to mint a ~60s bot token and call `match_chunks` as `bot_user_id` so that retrieval reuses the `auth.uid()` membership + owner-OR-ACL boundary wholesale and the bot answers only from share-to-bot documents (CONTEXT Support-bot principal; ADR-0008).
**Acceptance Criteria:**
- [ ] Per turn: mint bot token (US-068), call existing `match_chunks` RPC with that token, discard the token after the call — no caching of bot tokens across turns.
- [ ] No new retrieval predicate, no `principal_ids` passed from the backend — the DB resolves the bot's principal set from `auth.uid()` (CONTEXT principal-set resolution).
- [ ] The bot token is NEVER written to any SSE event, response body, or log line that reaches the client.
- [ ] Active-workspace filter passed as the ordinary non-security `match_chunks` param (resolved from the conversation's `workspace_id`), distinct from the trust boundary.
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Workspace W with doc D shared-to-bot and doc E NOT shared-to-bot. Provision bot B.
- **Steps:** 1. Run a turn whose answer lives in D → inspect retrieved chunk ids. 2. Run a turn whose answer lives only in E.
- **Expected Result:** Step 1 retrieves D's chunks; step 2 retrieves 0 chunks from E (bot has no grant). Bot token absent from all client-visible output.
- **Failure Indicator:** Bot retrieves E's chunks, or a bot token leaks into an SSE/response/log surface.

### US-071: Opaque per-conversation customer token — issuance, hashed storage, resume
**Description:** As an anonymous customer, I want an opaque random backend-issued token (NOT a Supabase JWT) bound to one `conversation_id`, stored hashed, with lifetime tracking the conversation so that I can reconnect across reloads while staying structurally off the Supabase trust surface (ADR-0008; amends ADR-0004).
**Acceptance Criteria:**
- [ ] Token is cryptographically-random opaque bytes, NOT signed with the JWT secret; only its hash is stored (e.g. `conversation_tokens(token_hash, conversation_id, expires_at, created_at)` or a hashed column on `conversations`); raw token returned once at conversation creation, only to the iframe.
- [ ] Lifetime ≈24h, refreshed on activity while `status != resolved`, invalidated on resolve; bound to exactly one `conversation_id`.
- [ ] A resume/revalidate endpoint accepts the opaque token, checks `not expired AND status != resolved`, and on success returns the conversation + allows SSE reconnect + transcript `GET`; no/expired/resolved → caller starts a fresh conversation on next first-message.
- [ ] There is NO server-side customer-identity table — continuity comes only from the iframe-origin-stored token.
- [ ] Typecheck/lint passes and migration applies.
**Validation Test (security-critical):**
- **Setup:** Conversation X (token Tx) and conversation Y (token Ty), different workspaces.
- **Steps:** 1. Call transcript/resume with Tx for X. 2. Call transcript/resume with Tx requesting Y's id. 3. Resolve X, then resume with Tx.
- **Expected Result:** Step 1 returns X only; step 2 returns **0 rows / rejected** (token bound to X, cannot read Y); step 3 rejected (invalidated on resolve).
- **Failure Indicator:** Tx reads Y or any conversation other than X, or a resolved conversation's token still resumes.

### US-072: `widget_keys` table + key resolution (validate-not-revoked before minting)
**Description:** As the widget runtime, I want `widget_keys` storing a non-secret public key per workspace and a resolution endpoint that validates not-revoked before any minting so that a public key resolves to `(workspace_id, bot_user_id)` and a revoked key cannot start new conversations (ADR-0008; CONTEXT widget-key mechanics).
**Acceptance Criteria:**
- [ ] Migration creates `widget_keys(id, workspace_id, public_key text unique, label text, allowed_origins text[], revoked_at timestamptz NULL, created_by uuid, created_at)`; public_key is non-secret (embedded in client JS).
- [ ] Multiple keys per workspace allowed; rotate = issue-new + revoke-old (no auto-rotation).
- [ ] Resolution validates `revoked_at IS NULL` before minting/creating anything; **revoking blocks NEW conversations but never terminates live ones** (the opaque per-conversation token is independent of the key once minted).
- [ ] First key issuance triggers lazy bot provisioning (US-069).
- [ ] `widget_keys` RLS restricts admin reads/writes to admins of the owning workspace (managed in `/support/settings`, US-093).
- [ ] Typecheck/lint passes and migration applies.
**Validation Test:**
- **Setup:** Workspace W with active key K and revoked key Kr; a live conversation C started under Kr before revocation.
- **Steps:** 1. Resolve K → start conversation. 2. Resolve Kr → attempt to start conversation. 3. Send a customer message on the pre-existing C (started under Kr).
- **Expected Result:** Step 1 succeeds; step 2 rejected (no new conversation, no minting); step 3 still works (live conversation survives revocation).
- **Failure Indicator:** A revoked key mints a session, or revoking a key kills an in-flight conversation.

### US-073: Per-key registered-domain allowlist, fail-closed (no domain = inactive)
**Description:** As a security-conscious operator, I want each widget key to carry a registered-origin allowlist that is **fail-closed** (a key with no registered domain is inactive) so that casual key-lifting and cross-site browser abuse are blunted as defense-in-depth (ADR-0008; not a hard control — key is non-secret, `Origin` is forgeable off-browser).
**Acceptance Criteria:**
- [ ] Resolution checks the request `Origin` against the key's `allowed_origins`; a request from an unlisted origin is refused.
- [ ] A key with `allowed_origins` empty/null is **inactive** (fail-closed, same posture as the embedder "refuse to start" guard); never fail-open.
- [ ] `*` wildcard is a documented dev-only opt-in, flagged non-production (F3 row).
- [ ] Code/docs frame this as defense-in-depth; the hard controls remain the rate limit + circuit breaker (US-076/077) and the leaked-key blast radius = already-public KB.
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** Key A with `allowed_origins=['https://client.example']`; key B with empty allowlist.
- **Steps:** 1. Resolve A with `Origin: https://client.example`. 2. Resolve A with `Origin: https://evil.example`. 3. Resolve B (no domain).
- **Expected Result:** Step 1 succeeds; step 2 **rejected**; step 3 **rejected** (inactive, fail-closed).
- **Failure Indicator:** An originless key is active, or an unlisted origin resolves successfully.

### US-074: Public widget CORS posture (separate from `FRONTEND_ORIGINS`)
**Description:** As the platform, I want public widget endpoints to have their OWN CORS posture, separate from the authenticated API's `allow_origins=FRONTEND_ORIGINS` (`main.py:~296`), so that the public surface honors only per-key registered origins and never inherits the authenticated app's origin trust (ADR-0008).
**Acceptance Criteria:**
- [ ] Public widget routes (key resolution, conversation create/message, transcript, customer SSE) are mounted under a distinct CORS configuration keyed off the per-key `allowed_origins`, NOT the global `CORSMiddleware(allow_origins=FRONTEND_ORIGINS)` used by `/api/*`.
- [ ] The authenticated `/api/*` CORS config is unchanged (no widening of `FRONTEND_ORIGINS`).
- [ ] Preflight (`OPTIONS`) on public endpoints reflects only the requesting key's allowed origin (or 403 when not allowed), never `*` outside dev opt-in.
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** `FRONTEND_ORIGINS=['https://app.kit']`; key K allows `https://client.example`.
- **Steps:** 1. Preflight a public widget endpoint with `Origin: https://client.example`. 2. Preflight same with `Origin: https://app.kit` (not in K's list). 3. Preflight `/api/chat` with `Origin: https://client.example`.
- **Expected Result:** Step 1 allowed (key origin); step 2 rejected (not K's origin even though it's an app origin); step 3 rejected (`/api/*` only trusts `FRONTEND_ORIGINS`).
- **Failure Indicator:** The public surface trusts `FRONTEND_ORIGINS`, or `/api/*` trusts a widget origin.

### US-075: Swappable `RateLimiter` seam (default Postgres, optional Redis adapter)
**Description:** As the platform, I want a `RateLimiter` ABC + factory with a Postgres-backed default (no new infra, durable, cross-instance) and an optional Redis adapter so that abuse counters mirror the existing `Reranker`/`WebSearchProvider`/`DocumentParser` factory pattern (ADR-0008).
**Acceptance Criteria:**
- [ ] A `RateLimiter` protocol/ABC with a Postgres implementation (counter rows / sliding-window in Postgres) selected by env (e.g. `RATE_LIMITER=postgres|redis`), default `postgres`.
- [ ] An optional Redis adapter stub implementing the same protocol (documented for scale); in-process memory rejected (per-instance under-count, resets on restart) — recorded in comment per ADR-0008.
- [ ] Migration adds the Postgres counter table(s) the default backend needs.
- [ ] Factory shape matches existing seams in the repo.
- [ ] Typecheck/lint passes and migration applies.
**Validation Test:**
- **Setup:** `RATE_LIMITER=postgres`.
- **Steps:** 1. Increment a counter for key Z N times within the window. 2. Restart the backend process. 3. Read the counter for Z.
- **Expected Result:** Counter survives the restart at value N (durable, not in-process); selecting `redis` swaps the adapter with no call-site change.
- **Failure Indicator:** Counter resets on restart, or swapping the adapter requires editing call sites.

### US-076: Per-key + per-session/IP sliding-window rate limit
**Description:** As the platform, I want a per-key and per-session/IP sliding-window request limit on the public widget endpoints so that the public surface (which drives paid retrieval + LLM draft/judge calls) cannot be turned into a cost-amplification DoS (ADR-0008 rate/abuse surface).
**Acceptance Criteria:**
- [ ] Each customer message / key-resolution request consumes from a per-key window AND a per-session/IP window via the `RateLimiter` seam.
- [ ] On limit breach the request is refused with a clear throttled response (no retrieval, no LLM call), distinct from the circuit-breaker deferral (US-077).
- [ ] Coarse cost accounting in v1 (requests × estimated per-request cost, or qps) — NOT precise token/dollar metering (documented future refinement, F3).
- [ ] App-level limiting is the portable default; an edge/WAF limiter is documented as recommended in production (P5 line).
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Key K with a low test window limit; one session S.
- **Steps:** 1. Send messages from S up to the limit. 2. Send one more from S. 3. Send from a fresh session S2 under the same key past the per-key window.
- **Expected Result:** Step 2 throttled (per-session); step 3 throttled by the per-key window even from a new session.
- **Failure Indicator:** No retrieval/LLM call is short-circuited on breach, or only one of the two windows enforces.

### US-077: Per-workspace circuit breaker → zero-cost deferral + operator Realtime badge
**Description:** As the platform, I want a per-workspace cost/qps circuit breaker that, when tripped, short-circuits to a generic deferral with NO retrieval and NO LLM call plus an in-app Realtime operator badge (zero outbound) so that a tripped breaker costs ~nothing (ADR-0008; same posture as escalation-notify).
**Acceptance Criteria:**
- [ ] A per-workspace ceiling (coarse qps/cost) tracked via the `RateLimiter` seam; when tripped, the turn returns a generic "high volume — a human will follow up" deferral with **no retrieval and no LLM call**.
- [ ] Tripping emits an in-app Realtime operator badge under the agent's real JWT — **zero outbound** (no ESP, no webhook), same posture as escalation-notify.
- [ ] A tripped breaker does NOT create a partial/garbage answer and does NOT count as a deflection.
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Workspace W with a low test per-workspace ceiling; instrument retrieval + LLM call counters.
- **Steps:** 1. Drive W past the ceiling. 2. Send another customer message. 3. Observe operator dashboard for W.
- **Expected Result:** Step 2 returns the generic deferral; retrieval-call and LLM-call counters do NOT increment for that turn; step 3 shows a Realtime badge; no outbound network call is made.
- **Failure Indicator:** A tripped breaker still calls retrieval/LLM, or emits an outbound notification.

### US-078: Lazy conversation creation on first customer message
**Description:** As the platform, I want a `conversations` row + opaque customer token + customer SSE created **lazily on the first customer message**, NOT on widget open, so that the public-page abuse surface is bounded to one-row-per-real-conversation (ADR-0008; CONTEXT lifecycle).
**Acceptance Criteria:**
- [ ] Widget open performs ONLY rate-limited key resolution — no `conversations` row, no customer token, no SSE.
- [ ] The first customer message creates the `conversations` row (`status='active'`, `bot_user_id` set, `channel='widget'`), issues the opaque token (US-071), opens the customer SSE, and runs the ADR-0003 pipeline.
- [ ] Reload with a valid stored token resumes the existing conversation (US-071) rather than creating a new row.
- [ ] No "row per pageview" — repeated opens without a message create zero rows.
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Fresh widget key K, no stored token.
- **Steps:** 1. Resolve K (open) 5 times without sending a message → count `conversations` rows. 2. Send first message → count rows. 3. Reload with stored token, send second message → count rows.
- **Expected Result:** Step 1 → 0 rows; step 2 → 1 row; step 3 → still 1 row (resumed, not duplicated).
- **Failure Indicator:** Any row created on open, or a reload duplicates the conversation.

### US-079: Deterministic deflection turn streams over the request-scoped SSE
**Description:** As an anonymous customer, I want the bot's own answer to stream over the request-scoped SSE my message opened (like `/api/chat`) so that I get a low-latency answer while `status='active'`, reusing the ADR-0003 deflection pipeline and the existing `_sse()` `delta`/`done`/`error` event shape (`main.py:474`).
**Acceptance Criteria:**
- [ ] A customer message on an `active` conversation runs the ADR-0003 deterministic deflection pipeline (retrieval gate → [if strong] draft → faithfulness gate) — implemented by the **escalation-pipeline section**; this story only invokes it and streams the result.
- [ ] Bot answer streams over the request-scoped SSE using the existing `_sse()` `delta`/`done`/`error` events; both customer message and bot answer are persisted to `conversation_messages` (face-agnostic streaming/draft/gate service writing to `conversation_messages` instead of `messages`, per ADR-0004 consequence).
- [ ] On the escalate branch, the pipeline emits no answer (generic deferral) and triggers the latch (US-080) — it does not stream a confident answer.
- [ ] `conversation_messages.tool_calls` stays null (deterministic pipeline, no agentic tool loop).
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Conversation C `active`; a question whose answer is in a shared-to-bot doc (P2).
- **Steps:** 1. Send the question. 2. Observe the SSE stream and `conversation_messages`.
- **Expected Result:** `delta` events stream a grounded answer then `done`; user + assistant rows persisted to `conversation_messages`; `tool_calls` null.
- **Failure Indicator:** The answer fails to stream over the request SSE, or persists to `messages`/`threads`.

### US-080: Escalation latch routes all later customer messages to the human
**Description:** As the platform, I want the first escalate decision to latch the conversation to `escalated` (bot goes silent, one-way) so that once a human is engaged the bot can never auto-send a confident wrong answer into the thread (ADR-0004; Risk #3).
**Acceptance Criteria:**
- [ ] The first turn whose ADR-0003 decision is escalate sets `status='escalated'` and `escalated_at=now()` (latch via US-067) and emits a generic deferral, not a bot answer.
- [ ] While `status='escalated'`, every later customer message is persisted to `conversation_messages` and routed to the human queue WITHOUT running the deflection pipeline (no retrieval, no draft, no LLM answer).
- [ ] User-initiated "talk to a human" (an explicit widget button, US-091) escalates via this same latch path — separate from the model-mediated decision.
- [ ] Typecheck/lint passes.
**Validation Test:**
- **Setup:** Conversation C `active`; instrument the deflection-pipeline entry.
- **Steps:** 1. Send a message that escalates (or click the human button). 2. Send two more customer messages. 3. Inspect pipeline-entry counter for steps 2's messages.
- **Expected Result:** After step 1, `status='escalated'`, `escalated_at` set; step 2 messages persist and appear in the queue but the pipeline-entry counter does NOT increment for them.
- **Failure Indicator:** Bot re-answers after escalation, or `escalated_at` is reset/unset on later turns.

### US-081: Customer SSE channel + LISTEN/NOTIFY fan-out
**Description:** As an anonymous customer, I want a long-lived backend SSE (authorized by my opaque token) over which async agent replies are pushed so that I receive a human reply without ever holding a Supabase JWT or touching Supabase Realtime — single-instance trivial, multi-instance via Postgres `LISTEN/NOTIFY` (no new infra) (ADR-0008; amends ADR-0004).
**Acceptance Criteria:**
- [ ] A `GET` customer SSE endpoint authorized by the opaque per-conversation token (US-071) holds a long-lived connection scoped to one `conversation_id`.
- [ ] A fan-out mechanism delivers a new `conversation_messages` row to the matching customer SSE: single-instance in-process registry; multi-instance uses Postgres `LISTEN/NOTIFY` keyed by `conversation_id` (no Redis/queue infra added).
- [ ] The customer SSE never carries any data from other conversations; the connection closes/refuses when the token is invalidated (resolve).
- [ ] The anonymous customer is structurally OFF the Supabase JWT/Realtime surface (no supabase-js channel for the customer leg).
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** Two conversations X (token Tx), Y (token Ty), possibly on different backend instances.
- **Steps:** 1. Open customer SSE for X with Tx. 2. Insert an agent reply into Y. 3. Insert an agent reply into X.
- **Expected Result:** X's SSE receives ONLY the X reply (step 3); the Y reply (step 2) is **never** delivered to X's SSE (0 cross-conversation messages).
- **Failure Indicator:** X's SSE receives a Y message, or the customer leg subscribes to Supabase Realtime.

### US-082: Agent-reply endpoint (`POST /widget/conversations/{id}/agent-reply`)
**Description:** As a workspace agent, I want `POST /widget/conversations/{id}/agent-reply` (authed by my real workspace JWT) to write `conversation_messages` AND fan the message down the matching customer SSE so that my reply reaches the anonymous customer through the backend, not Supabase Realtime (ADR-0008; amends ADR-0004).
**Acceptance Criteria:**
- [ ] Endpoint authed by the agent's real Supabase JWT via the existing `get_user` path (`main.py:361`); the agent must be a member of the conversation's workspace (membership RLS on the write, US-066).
- [ ] Writes the `(role='assistant'/'agent', content)` row to `conversation_messages` under the agent's JWT, then triggers the customer-SSE fan-out (US-081) for that `conversation_id`.
- [ ] Reply is permitted on `escalated` conversations (and is the only message source post-latch); a non-member's JWT is rejected.
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** Conversation C in workspace W (`escalated`); agent A1 ∈ W, agent A2 ∉ W; customer SSE open for C.
- **Steps:** 1. A1 posts an agent reply to C. 2. A2 (cross-workspace JWT) posts a reply to C.
- **Expected Result:** Step 1 writes `conversation_messages` and the reply arrives on C's customer SSE; step 2 **rejected / 0 rows written** (A2 not a member of W).
- **Failure Indicator:** A2's cross-workspace reply succeeds, or A1's reply fails to fan out to the customer.

### US-083: Loader `<script>` + cross-origin iframe widget shell (host↔widget postMessage)
**Description:** As a buyer, I want a tiny loader `<script>` that injects a launcher bubble + a cross-origin iframe served from the kit's own origin, communicating with the host page via `postMessage` only, so that the host page's JS (or an XSS on it) cannot read the widget's token (ADR-0008 max-isolation embed).
**Acceptance Criteria:**
- [ ] A small loader script injects a launcher + an iframe whose `src` is the kit's own origin (NOT the host origin); the conversation token + session live in the iframe's own-origin `localStorage`, unreadable by host JS.
- [ ] Host↔widget communication is `postMessage` only: `open`/`close`, unread-badge, and `init` config — no shared globals, no host-CSS reaching the iframe.
- [ ] Loader accepts the non-secret public key + init config and forwards them to the iframe via `postMessage`.
- [ ] Web-component/shadow-DOM embed explicitly rejected (JS-shared; host could read the token) — noted in code/docs.
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (loader injects iframe, launcher opens/closes via postMessage, token not visible from host `window`/`document`).
**Validation Test (security-critical):**
- **Setup:** A test host page on `http://host.local` embedding the loader; widget served from the kit origin; start a conversation so a token is stored.
- **Steps:** 1. Open the widget, send a message (token gets stored in iframe origin). 2. From the host page's JS console, attempt to read the iframe's `localStorage` / the token. 3. Toggle open/close via the launcher.
- **Expected Result:** Host JS **cannot** read the iframe's `localStorage` (cross-origin, throws/empty); launcher open/close works via postMessage.
- **Failure Indicator:** Host JS reads the customer token, or theming/state crosses without postMessage.

### US-084: Widget chat UI + theming via loader `init` config (no host-CSS)
**Description:** As a buyer, I want the in-iframe chat UI (message list, composer, launcher, streaming) themed via the loader's `init` config (brand color, position, greeting, launcher icon) over `postMessage` so that I get a controlled branded surface without host-CSS bleed (ADR-0008; CONTEXT theming).
**Acceptance Criteria:**
- [ ] In-iframe chat UI renders streamed `delta` tokens (US-079) and agent replies arriving on the customer SSE (US-081); composer disabled appropriately when throttled/breaker-tripped.
- [ ] Theming knobs (brand color, position, greeting, launcher icon) applied from the `init` config; admin sets per-key defaults in `/support/settings` (US-093), loader may override; **no host-CSS theming** (the iframe forecloses it).
- [ ] Unread-badge state surfaced to the host via `postMessage` when closed and a new agent reply arrives.
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (send a message, see streamed answer; apply a brand color + greeting via init and see them; receive an agent reply live).
**Validation Test:**
- **Setup:** Loader configured with `brandColor`, `greeting`, `position='bottom-left'`.
- **Steps:** 1. Open widget → see greeting + brand color + position. 2. Send a message → see streamed answer. 3. From the agent side post a reply → see it appear live; close widget → unread badge shows.
- **Expected Result:** Theming reflects init config; streaming + live agent reply render; unread badge updates via postMessage.
- **Failure Indicator:** Theming requires host CSS, or agent reply does not render live.

### US-085: Session-scoped retrieval memory enforcement (transcript ≠ retrievable)
**Description:** As a security property, I want the bot's retrieval/answerable surface to be session-scoped — customer input NEVER becomes a retrievable chunk and never bleeds across sessions — distinct from the durable agent-readable transcript (`conversation_messages`, never fed back into retrieval) (ADR-0004 + CONTEXT "two stores, one word").
**Acceptance Criteria:**
- [ ] Customer messages are persisted to `conversation_messages` (durable, agent-readable) but are NEVER embedded/inserted into `chunks` / the retrievable corpus.
- [ ] The bot's retrieval call (US-070) only reads `match_chunks`; it never reads `conversation_messages` from other conversations and never reads prior-session content as a retrieval source.
- [ ] No code path turns customer-pasted text into a chunk_acl-granted chunk.
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** Conversation A where the customer pastes a unique sentinel string; conversation B (different session, same workspace).
- **Steps:** 1. In A, paste the sentinel. 2. Inspect `chunks` for the sentinel. 3. In B, ask a question that would surface the sentinel if it were retrievable.
- **Expected Result:** Sentinel appears in `conversation_messages` only; **0** matching rows in `chunks`; B's retrieval returns **0** chunks containing the sentinel (no cross-session bleed).
- **Failure Indicator:** Customer input becomes a retrievable chunk, or surfaces in another session's retrieval.

### US-086: Share-to-bot as a separate, explicitly-confirmed publish action
**Description:** As a doc owner, I want "publish to support bot" to be a separate, explicitly-confirmed action framed as publishing to the **public** widget — NOT a quiet grantee row in the existing share dialog — so that I can't accidentally make a doc's synthesized faithful answer customer-reachable by typing the bot's email into the normal grant box (ADR-0008; CONTEXT share-to-bot).
**Acceptance Criteria:**
- [ ] Sharing to the bot grants via the existing `chunk_acl` mechanism (one row per chunk, bot as `user` principal) — but ONLY through a distinct, explicitly-confirmed "publish to public support widget" UX, not the normal grant input.
- [ ] The bot cannot be added by typing its email into the standard share dialog's grant box (the email→principal resolution excludes/blocks the bot user, or the bot is not surfaced there).
- [ ] The confirm step states the consequence: contents become answerable to anyone who can reach the widget (the leak vector is the synthesized faithful answer).
- [ ] Managed under `/support/settings` (US-093) and/or the doc's share surface as a clearly separated action.
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (bot absent from normal grant box; publish-to-bot is a separate confirmed action).
**Validation Test (security-critical):**
- **Setup:** Doc D, support bot B provisioned in the workspace.
- **Steps:** 1. In the normal share dialog, type B's email and try to grant. 2. Use the explicit publish-to-bot action on D and confirm.
- **Expected Result:** Step 1 cannot grant to the bot (blocked/not resolvable); step 2 creates the `chunk_acl` bot grants only after explicit confirmation; D's answer is then bot-retrievable (US-070).
- **Failure Indicator:** Typing the bot email in the normal box silently shares to the public widget.

### US-087: `/support/queue` route — membership-gated escalated list, agent Realtime
**Description:** As any workspace member, I want `/support/queue` to list `status='escalated'` conversations for the active workspace, live via my own Supabase Realtime under my real JWT, so that I can pick up handoffs without crossing the tenant boundary (ADR-0004/0008; membership-gated, NOT role-gated).
**Acceptance Criteria:**
- [ ] New authenticated in-app route `/support/queue` (added to `App.tsx` Routes under `ProtectedRoute`), membership-gated — readable/actionable by ANY member of the active workspace, `role` in no gate.
- [ ] Lists `status='escalated'` conversations for the active workspace (default-when-sole / 400-on-ambiguous active-workspace resolution); live-updates via the agent's own Supabase Realtime `postgres_changes` (the existing authenticated pattern in `frontend/src/lib/ingestion.ts:225`).
- [ ] No cross-workspace inbox in v1 (single active workspace per view).
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (escalated conversation appears live in the queue for a member).
**Validation Test:**
- **Setup:** Member U1 ∈ W; conversation C in W escalates while U1 has `/support/queue` open. Member U2 ∉ W.
- **Steps:** 1. Escalate C. 2. Observe U1's queue. 3. Load `/support/queue` as U2.
- **Expected Result:** C appears in U1's queue live via Realtime; U2 (not a member of W) sees 0 of W's conversations.
- **Failure Indicator:** The queue gates on `role`, requires a manual refresh, or shows cross-workspace conversations.

### US-088: Queue conversation view — read transcript, reply, Resolve
**Description:** As an agent, I want to open an escalated conversation, read its full transcript, post a reply (via the agent-reply endpoint), and click Resolve so that I can handle the handoff and close it (ADR-0004 queue posture).
**Acceptance Criteria:**
- [ ] Opening a conversation renders the full `conversation_messages` transcript (read under the agent's real JWT + membership RLS).
- [ ] Reply posts to `POST /widget/conversations/{id}/agent-reply` (US-082) → backend writes + SSE-fans to the customer.
- [ ] **Resolve** sets `status='resolved'` (terminal, US-067), which invalidates the customer token (US-071); the customer SSE for it closes.
- [ ] `tool_calls` tree is not rendered (null/unused for widget convos).
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (open transcript, send reply that reaches the widget, Resolve moves it out of the queue).
**Validation Test:**
- **Setup:** Escalated conversation C with a transcript; widget open on the customer side.
- **Steps:** 1. Open C, read transcript. 2. Send an agent reply. 3. Click Resolve. 4. Customer attempts to resume with the stored token.
- **Expected Result:** Transcript renders; reply appears in the widget live; Resolve sets `resolved` and removes C from the queue; step 4 resume is rejected (token invalidated on resolve).
- **Failure Indicator:** Reply doesn't reach the widget, Resolve doesn't invalidate the token, or a resolved conversation stays in the queue.

### US-089: Optional unenforced soft-claim (`claimed_by`/`claimed_at`)
**Description:** As an agent on a shared queue, I want an optional soft-claim (`claimed_by`/`claimed_at`, last-write-wins, advisory) that dims a claimed row so that two agents are less likely to double-reply — without it being a routing/assignment axis (ADR-0004/0008; S5 stays deferred).
**Acceptance Criteria:**
- [ ] Claiming sets `claimed_by=auth.uid()`, `claimed_at=now()` on the conversation; **unenforced** — last-write-wins, anyone may still reply (advisory only).
- [ ] The queue UI dims/marks a claimed row with the claimer's identity; claim is not a hard assignment and does NOT gate the reply/resolve actions.
- [ ] No team routing (S5 deferred).
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (claiming dims the row for other agents; a non-claimer can still reply).
**Validation Test:**
- **Setup:** Escalated conversation C; agents A1, A2 ∈ W, both with the queue open.
- **Steps:** 1. A1 claims C. 2. A2's queue view. 3. A2 replies to C anyway.
- **Expected Result:** C dims in A2's view showing A1 as claimer; A2's reply still succeeds (unenforced).
- **Failure Indicator:** Claim blocks A2's reply (would make it an enforced assignment, out of scope).

### US-090: `/support/settings` route — admin-gated (enable-support, keys, share-to-bot)
**Description:** As a workspace admin, I want `/support/settings` (role=admin-gated) to enable support / provision the bot, manage widget keys (issue/rotate/revoke + per-key origins + theming defaults), and manage share-to-bot so that the administrative actions ADR-0002's admin role is for live in one place (ADR-0004/0008).
**Acceptance Criteria:**
- [ ] New authenticated route `/support/settings` (added to `App.tsx`), gated on `role='admin'` of the active workspace (these ARE the administrative actions, distinct from the membership-gated queue).
- [ ] Enabling support provisions the bot lazily (US-069) on first key issuance; admins issue keys with label + registered origins + theming defaults; rotate = issue-new + revoke-old; revoke flips `revoked_at`.
- [ ] Surfaces the per-key origin allowlist (fail-closed reminder if empty) and the `*` dev-only warning; manages share-to-bot (US-086).
- [ ] Active-workspace-scoped (default-when-sole / 400-on-ambiguous).
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (admin can issue/rotate/revoke a key; non-admin member is blocked from the settings route).
**Validation Test:**
- **Setup:** Admin Ad ∈ W (role=admin); plain member Me ∈ W (role=member).
- **Steps:** 1. Ad issues a key with one origin. 2. Ad rotates it (issue-new + revoke-old). 3. Me loads `/support/settings`.
- **Expected Result:** Ad can issue/rotate; the old key shows revoked, the new key active; Me is blocked from `/support/settings` (admin-gated) but can still reach `/support/queue` (membership-gated).
- **Failure Indicator:** A non-admin reaches settings, or rotation doesn't revoke the old key.

### US-091: User-initiated "talk to a human" widget button
**Description:** As a customer, I want an explicit "talk to a human" button in the widget so that I can request handoff directly (a separate concern from the model-mediated decision; an explicit widget control, never a model tool) (CONTEXT escalation; ADR-0003).
**Acceptance Criteria:**
- [ ] The widget exposes an explicit "talk to a human" control that escalates the conversation via the same latch path (US-080) — `status='escalated'`, `escalated_at` set, bot silent thereafter.
- [ ] This is NOT model-mediated (no `escalate()` model tool); it is a deterministic UI-initiated escalation.
- [ ] After clicking, the customer's next messages route to the human queue and the customer awaits a reply over the SSE.
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (clicking the button latches escalation; conversation appears in the queue).
**Validation Test:**
- **Setup:** Active conversation C.
- **Steps:** 1. Click "talk to a human". 2. Send another message. 3. Check the queue.
- **Expected Result:** C latches to `escalated`; the next message routes to the queue with no bot answer; C appears in `/support/queue`.
- **Failure Indicator:** The bot keeps answering after the button, or escalation goes through a model tool.

### US-092: `customer_email` collected at escalation (optional, never a gate)
**Description:** As a customer, I want to optionally leave my email at the point of escalation ("leave your email and a human will follow up") so that an agent can follow up manually if I leave — never a pre-chat email wall and never required to escalate (ADR-0004; v1 no ESP).
**Acceptance Criteria:**
- [ ] An optional email field is surfaced AT escalation (not before; no pre-chat email wall); escalation proceeds with or without it.
- [ ] `customer_email` is stored on `conversations` as metadata only — NEVER a retrieval principal; shown to the agent in the queue for manual follow-up.
- [ ] v1 sends NO automated email (no ESP integration); the field is for manual follow-up.
- [ ] Typecheck/lint passes.
- [ ] **Verify in browser using dev-browser skill** (email prompt appears only at escalation; escalation succeeds when left blank).
**Validation Test:**
- **Setup:** Active conversation C.
- **Steps:** 1. Escalate C without entering an email. 2. Escalate a second conversation with an email entered. 3. View both in the queue.
- **Expected Result:** Both escalate; the first has null `customer_email`, the second shows the email to the agent; no outbound email is sent in either case.
- **Failure Indicator:** Escalation is blocked when email is blank, or an email is auto-sent, or a pre-chat email wall appears.

### US-093: AU4 widget/conversation attack tests (assert-style)
**Description:** As the security gate, I want AU4 to gain widget/conversation attack cases asserting exact zero/rejected outcomes so that the new public surface and customer token cannot leak across the tenant or conversation boundary (ADR-0008 + ADR-0004 AU4 additions; pinned `fail` security invariant per E8).
**Acceptance Criteria:**
- [ ] Test: a cross-workspace real JWT retrieves **0** conversations / 0 `conversation_messages` from another workspace (US-066 RLS).
- [ ] Test: a per-conversation opaque token reads **only its own** conversation — **0 rows** for any other conversation (US-071/US-081).
- [ ] Test: a **revoked or originless key mints no session** — no conversation created, no token issued (US-072/US-073).
- [ ] Test: a **customer token retrieves 0 chunks** — the customer is structurally off retrieval; only the server-side bot token can call `match_chunks`.
- [ ] These run as deterministic `assert == 0 / rejected` invariants on the pinned security gate (E8), placed with the existing AU4 suite; not buyer-downgradable.
- [ ] Typecheck/lint passes.
**Validation Test (security-critical):**
- **Setup:** W1 (conv X) + W2 (conv Y); valid key K, revoked key Kr, originless key Ko; bot token Tb, customer token Tx.
- **Steps:** 1. W2-member JWT lists conversations. 2. Tx reads Y. 3. Resolve Kr and Ko to start a conversation. 4. Tx (customer token) calls `match_chunks`.
- **Expected Result:** (1) 0 of W1's conversations; (2) **0 rows** for Y; (3) **no session minted** for either Kr or Ko; (4) customer token retrieves **0 chunks** (not a Supabase JWT / not a retrieval principal).
- **Failure Indicator:** Any case returns >0 rows / mints a session / retrieves a chunk.

#### Functional requirements (this area)
- FR-D1: New `conversations` + `conversation_messages` tables with `viewer ∈ members(workspace_id)` RLS (the ADR-0002 clause, `role` in no predicate); NOT an extension of `threads`/`messages`, which stay untouched.
- FR-D2: Status machine `active → escalated → resolved` with a one-way escalation latch (`escalated_at` set-once); deflection is DERIVED (`resolved AND escalated_at IS NULL`), not a stored status; `resolved` is terminal and invalidates the customer token.
- FR-D3: One minting primitive — self-signed HS256 Supabase-compatible JWTs with the project JWT secret; (a) **bot token** `sub=bot_user_id`, ~60s, server-side only, per-turn, to call `match_chunks`; (b) **customer token** opaque (not a Supabase JWT), conversation-bound, hashed at rest, the only token shipped to the iframe.
- FR-D4: Per-workspace support bot provisioned lazily on first key issuance — `role='member'` + `is_bot` flag, one per workspace, no new content role; bot sees only share-to-bot grants via `chunk_acl`.
- FR-D5: `widget_keys` with per-key fail-closed origin allowlist; public widget CORS posture separate from `FRONTEND_ORIGINS`; revoke blocks new conversations, never terminates live ones.
- FR-D6: Swappable `RateLimiter` seam (default Postgres) → per-key + per-session/IP sliding window AND per-workspace circuit breaker (zero-cost generic deferral + in-app Realtime badge, zero outbound).
- FR-D7: Lazy conversation creation on first message; bot answer over the request-scoped SSE; async agent reply over a long-lived backend customer SSE with `LISTEN/NOTIFY` fan-out; agent-reply endpoint authed by the agent's real workspace JWT.
- FR-D8: Cross-origin iframe + loader `<script>` (host↔widget postMessage only), theming via loader `init` config (no host-CSS); session-scoped retrieval memory (transcript ≠ retrievable); share-to-bot as a loud, separate, explicitly-confirmed publish action.
- FR-D9: In-app operator routes — `/support/queue` (membership-gated, agent Realtime, reply, Resolve, optional unenforced soft-claim) and `/support/settings` (admin-gated, keys, enable-support, share-to-bot); both active-workspace-scoped, no cross-workspace inbox.
- FR-D10: AU4 gains assert-style widget/conversation cases (cross-workspace JWT → 0 conversations; per-conversation token → only its own; revoked/originless key → no session; customer token → 0 chunks).

#### Non-goals (this area)
- S5 teams / assignment routing; P1b access-aware detection (no privileged second pass in code; P1b rows expect P1a output trivially); ESP / automated email auto-send; outbound webhook / Slack notify; idle-sweep cron (fully-deflected conversations stay `active` forever in v1); cross-workspace unified operator inbox; cross-device / cleared-storage customer identity (no server-side customer-identity table); host-CSS widget theming; web-component / shadow-DOM embed; Supabase Realtime for the customer leg (backend-SSE only); precise token/dollar cost metering (coarse qps/cost only); per-workspace trace RLS (operator-only in v1); `restricted-to:` hints on the handoff surface (P1b deferred); restricting customer-pasted-PII visibility to a role/agent sub-gate (config-shaped future, documented wart).
## F. Eval gate + golden-set genericization (ADR-0005)

E8/E9/E10 turn the project's proven eval suite into *buyer methodology*: a configurable CI gate, a buyer-authored layered golden set, and a generic corpus seeder — without cracking the leak/correctness proof that is the product thesis (Risk #3). The gate is **not** one flat `off|comment|fail` knob (PRD A5 is refined, not adopted as written): eval outputs split into a **pinned security/correctness class** (`assert leak==0`, never buyer-downgradable) and a **tunable quality/regression class** (`off|comment|fail` over the existing `red`/`yellow` severity model), with `false-resolve` straddling — a quality metric whose buyer-set ceiling is a pinned invariant. The detection layer in `evals/retrieval/ragas_gates.py` (operational floors, rolling-median windows, cross-family corroboration, per-tag auto-close) is kept **wholesale**; only its project-specific *bindings* (cells, `CLAUDE_EQUIVALENT`, `CLAUDE_JUDGE_CELL`, threshold constants) move into a buyer-authored gate declaration. Gold labels become **content anchors** (answer-bearing text resolved to current chunks at eval time, fail-loud on zero-resolve) so a buyer sweeps `chunk_size`/overlap/docling with zero re-labeling, and the seeder seeds a corpus and nothing eval-specific (synthetic viewers built transiently by the runner, never baked into a production seed).

### US-101: Classify every eval output into a gate class
**Description:** As the kit author, I want every eval output tagged as either a `security` invariant or a `quality` metric (with `false-resolve` flagged as a straddling metric) so that the gate applies pinned-fail semantics to correctness proofs and tunable semantics to judgment-call metrics, never letting a buyer downgrade the leak proof.
**Acceptance Criteria:**
- [ ] A `gate_class` registry (new module, e.g. `evals/gate/classes.py`) enumerates each eval output with `class ∈ {security, quality}` and a `determinism ∈ {deterministic, non_deterministic}` flag (orthogonal axis — see US-104).
- [ ] `security` members: E4 zero-leak (the `security_no_access` table in `runner.py::_aggregate_viewer_filter` must read 1.0 / 0 gold for `no_access` under both filters), E6 workspace-boundary assertion, AU4 API-layer auth-attack tests, E7 P1b non-disclosure (customer-facing P1b output ≡ P1a).
- [ ] `quality` members: `recall_at_k` / `mrr` / `ndcg_at_5` (per `runner.py` metrics), the four RAGAS scores (`RAGAS_METRICS` in `ragas.py`), escalation `deflection_rate` and `false_escalate_rate` — but **NOT** `false_resolve`.
- [ ] `false_resolve` is registered as `quality` with a `straddle: ceiling_is_invariant` marker so US-103's verdict layer treats a ceiling breach as a hard fail (see US-103).
- [ ] The registry asserts at import time that no `security` member carries an `off`/`comment` setting (US-102 enforces this); a `security` row with a loudness knob is a build error.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Import the gate-class registry in a unit test.
- **Steps:** 1. Assert `gate_class("E4_zero_leak").class == "security"` and that querying its loudness knob raises. 2. Assert `gate_class("recall_at_5").class == "quality"`. 3. Assert `gate_class("false_resolve").class == "quality"` and `.straddle == "ceiling_is_invariant"`. 4. Assert `deflection_rate` and `false_escalate_rate` are `quality` while `false_resolve` is not silenceable.
- **Expected Result:** Every output resolves to exactly one class; security members reject loudness config; `false_resolve` is quality-but-pinned-ceiling.
- **Failure Indicator:** Any eval output is unclassified, a security member accepts `comment`/`off`, or `false_resolve` is treated as a plain tunable metric.

### US-102: Pin the security/correctness gate to `fail` — silence only by deletion
**Description:** As a buyer's security reviewer, I want the E4/E6/AU4/E7-P1b invariants to be impossible to configure to `comment` or `off` so that "the security gates cannot be turned off, and here is the eval that proves it" is true — silencing one requires *deleting* the eval (a loud, auditable diff), never a quiet config flag.
**Acceptance Criteria:**
- [ ] The gate declaration loader rejects (hard error, non-zero exit) any attempt to set a verdict (`off`/`comment`/`fail`) on a `security`-class output — these are pinned `fail`, not present in the tunable verdict map.
- [ ] Security invariants are evaluated as binary `assert`s (e.g. `security_no_access[filter][mode] == 1.0` for every `no_access` cell), not as a threshold-comparison a buyer can loosen.
- [ ] A breach exits the run non-zero on the trigger where the invariant runs (deterministic ones per-PR — US-104), independent of any buyer verdict config.
- [ ] The only way to stop a security invariant from running is to remove its eval/golden labels from the repo (a tracked deletion), which the guide (US-110) documents as the sole, deliberately loud, escape hatch.
- [ ] A unit/integration test proves a gate-declaration YAML that tries `E4_zero_leak: comment` fails to load with an actionable error.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Author a gate declaration that sets `E4_zero_leak: off` and another that sets `e6_workspace_boundary: comment`.
- **Steps:** 1. Load each declaration through the gate loader. 2. Run the gate against a fixture where `no_access` leaks one gold chunk. 3. Confirm the loader rejected the downgrade and the leak fixture still exits non-zero.
- **Expected Result:** Both declarations fail to load with a "security gates are pinned `fail` and cannot be downgraded; delete the eval to remove it" message; the leak fixture fails the build regardless.
- **Failure Indicator:** A security invariant can be set to `comment`/`off`, or a known leak fixture merges green.

### US-103: Extract project bindings into a buyer-authored gate declaration (keep detection wholesale)
**Description:** As a buyer, I want the project-specific constants currently hardcoded in `ragas_gates.py` to live in a declarative gate file beside my golden set so that I can describe *my* cells, judge map, and thresholds without forking the detection logic — and the rolling-window / cross-family / auto-close machinery keeps working unchanged.
**Acceptance Criteria:**
- [ ] A new gate-declaration schema (e.g. `evals/gate/gate.yaml` beside the golden set) carries the bindings lifted from `ragas_gates.py`: the cell list (today `RAGAS_CELL_IDS`), the metric→judge-equivalent map (`CLAUDE_EQUIVALENT`), the cross-family judge cell (`CLAUDE_JUDGE_CELL`), and the threshold constants (`COVERAGE_FLOOR`, `API_ERROR_CEILING`, `RAGAS_DROP`, `CLAUDE_*_DROP`, `MIN_*_HISTORY`).
- [ ] `ragas_gates.py` detection functions (`check_operational_gates`, `check_diagnostic_gates`, `check_score_regressions`) are refactored to take these bindings as parameters/config rather than module constants — the *algorithms* (fixed floors, rolling-median windows, cross-family corroboration, `single-judge-red`, severity, `auto_close_weeks`) are unchanged.
- [ ] The kit ships a default `gate.yaml` whose values reproduce today's constants exactly, so the existing weekly behavior is byte-identical (regression guard).
- [ ] The corroboration binding (`generator_family`, `judge_family`, judge-equivalent map) is **optional**: when `judge_family == generator_family` or it is omitted, score regressions degrade to the existing `single-judge-red` path (red, `auto_close_weeks=2`, tagged) — no new code (US-109 covers the guide's framing).
- [ ] Loading a declaration with an unknown cell or metric name is a hard error (no silent skip).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Run `check_score_regressions` twice — once with the legacy hardcoded constants, once driven by the default `gate.yaml` — over the same history fixtures.
- **Steps:** 1. Build identical RAGAS-drop + corroborated-Claude-drop fixtures. 2. Run both code paths. 3. Compare the `GateFinding` lists (severity, tag, cross_family_corroborated, auto_close_weeks).
- **Expected Result:** The declaration-driven path produces findings identical to the legacy constants; omitting the corroboration binding turns a would-be cross-family red into `single-judge-red`.
- **Failure Indicator:** A binding change alters detection-algorithm behavior, or the genericized path diverges from the frozen baseline on the default declaration.

### US-104: Verdict layer — `off|comment|fail` as a per-suite loudness knob over severity
**Description:** As a buyer, I want one per-suite `off|comment|fail` knob that maps the existing `red`/`yellow` severity onto an action so that the two postures the repo already ships (weekly-fails-on-red, PR-comments-only) become two values of one config, and I never have to touch the detector to change loudness.
**Acceptance Criteria:**
- [ ] A verdict function maps `(severity, knob) → action`: `fail` ⇒ red fails / yellow comments; `comment` ⇒ red and yellow both comment, nothing blocks; `off` ⇒ nothing posts; default `comment`.
- [ ] The knob is **per quality suite** (retrieval-metrics suite, RAGAS suite, escalation suite), not a single global flag — it does not flatten the detection layer (US-103) and does not apply to security-class outputs (US-102) or to `false_resolve`'s ceiling (US-103/US-101).
- [ ] `false_resolve` ceiling breach maps to `fail` regardless of the escalation suite's loudness knob (the buyer picks the ceiling *value*; they cannot configure the gate to ignore a breach of their own tolerance).
- [ ] The existing weekly red→exit-non-zero behavior (`runner.py::amain` red-findings path) is reproduced when the weekly suite's knob is `fail`; the existing PR comment-only behavior (`ci/diff_results.py`) is reproduced when the PR suite's knob is `comment`.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A red `GateFinding` and a yellow `GateFinding` fixture.
- **Steps:** 1. Apply verdict with knob `fail` → assert the red yields a blocking action and the yellow a comment action. 2. Apply with `comment` → both yield comment actions, none blocking. 3. Apply with `off` → no action. 4. Apply a `false_resolve` ceiling-breach finding under knob `comment` → assert it still blocks.
- **Expected Result:** Loudness changes the action surface only; detection output (the findings) is identical across knob values; the `false_resolve` ceiling ignores the knob.
- **Failure Indicator:** A knob value changes which findings are *detected*, security/`false_resolve` invariants become silenceable, or a knob fails to reproduce a shipped posture.

### US-105: Determinism-boundary CI placement — per-PR block-merge vs scheduled file-issue
**Description:** As the kit author, I want the determinism axis (not buyer preference) to decide what may block a merge so that deterministic gates can hard-fail a PR while LLM-judged gates live only on scheduled runs — a judge wobble must never red-bar an innocent merge.
**Acceptance Criteria:**
- [ ] **Deterministic** gates (recall@k / MRR / nDCG, the pinned E4/E6/AU4 invariants, and the deterministic retrieval-gate tripwire — top-1 cosine `< τ_sim` / fewer than `N` cleared, pure arithmetic) run **per-PR** and a `fail` verdict **blocks the merge** (non-zero exit on the `pull_request` workflow).
- [ ] **Non-deterministic** gates (the four RAGAS scores, the runtime faithfulness gate, the full E7 deflection/false-resolve sweep) are structurally placed on **scheduled** workflows (weekly RAGAS, nightly, perms-scale); the config offers **no** per-PR `fail` for them. Their `fail` = fail the scheduled workflow + file one issue per tag (today's `retrieval-eval-ragas-weekly.yml` behavior), never block a merge.
- [ ] The current four-workflow split is formalized to one rule: `retrieval-eval.yml` (per-PR, deterministic metrics + security invariants, may block); `retrieval-eval-ragas-weekly.yml` / `retrieval-eval-nightly.yml` / `permissions-scale-eval.yml` (scheduled, judge-driven, file-issue-on-fail).
- [ ] A new per-PR step runs the deterministic security invariants (E4/E6/AU4) and the deterministic retrieval-leg tripwire and exits non-zero on breach — so a cross-workspace leak introduced by a PR fails before merge.
- [ ] The gate-declaration loader rejects a config that requests per-PR `fail` on a `non_deterministic` gate (a structural error, not a runtime one).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A PR fixture that introduces a `no_access` leak (deterministic) and a separate fixture that lowers a RAGAS faithfulness score (non-deterministic).
- **Steps:** 1. Run the per-PR gate over the leak fixture → assert non-zero exit (merge blocked). 2. Attempt to configure RAGAS faithfulness as per-PR `fail` → assert the loader rejects it. 3. Run the scheduled gate over the RAGAS-drop fixture → assert it fails the scheduled workflow and emits a file-issue action, not a merge block.
- **Expected Result:** Deterministic breaches block the PR; judge-driven regressions are caught only on the scheduled run and file an issue.
- **Failure Indicator:** An LLM-judged gate can be set to per-PR `fail`, or a deterministic leak does not block the merge.

### US-106: Document the false-resolve faithfulness-leg latency gap
**Description:** As a buyer evaluating safety guarantees, I want the accepted detection-latency gap on `false_resolve` documented in the threat model and capability matrix so that I understand the retrieval leg is caught per-PR but the faithfulness leg is merge-then-catch (up to a week).
**Acceptance Criteria:**
- [ ] The buyer threat model (P5) and capability matrix (F3) carry a row: *safety-critical faithfulness regressions (E7 P3, strong retrieval / unfaithful draft) have up-to-a-week detection latency, mitigated only by the deterministic retrieval-leg tripwire on the merge path.*
- [ ] The doc states the split explicitly: the deterministic per-PR retrieval-gate tripwire catches the **retrieval leg** (weak-retrieval escalations) immediately; the faithfulness leg is LLM-judged and only on the scheduled sweep.
- [ ] The rejected alternative (a blocking per-PR LLM-faithfulness gate) is noted with its reason (flaky merges + per-push judge spend, wrong for a starter kit).
- [ ] Typecheck/lint passes (docs-only; lint = markdown/link check)
**Validation Test:**
- **Setup:** Open the threat-model and F3 capability-matrix docs.
- **Steps:** 1. Grep for the faithfulness-latency gap row. 2. Confirm it names the retrieval-leg tripwire as the per-PR mitigation and the weekly sweep as the faithfulness-leg catch.
- **Expected Result:** Both docs carry the gap as an explicit accepted-gap row consistent with F3's standing-sink discipline.
- **Failure Indicator:** The latency gap is undocumented or implies false-resolve is fully caught per-PR.

### US-107: Content-anchor gold-label resolver (fail-loud on zero-resolve)
**Description:** As a buyer iterating on chunking, I want to author gold labels as answer-bearing text (a quoted span / stable doc + locator) that the harness resolves to whichever chunk(s) currently contain it at eval time so that I can sweep `chunk_size`/overlap/docling with **zero re-labeling** — and a span that matches no chunk is a hard error, never a silent `recall=0`.
**Acceptance Criteria:**
- [ ] The golden-set schema accepts a content anchor per gold label (quoted answer-bearing span, optionally scoped by stable document + locator), replacing the authored `gold_stable_ids` chunk-index primitive in `retrieval_gold.yaml`.
- [ ] A resolver maps each anchor to the current chunk `stable_id`(s) containing its text, at eval time (the `{filename_slug}:{chunk_index}` `stable_id` survives only as the *resolved internal* representation, never authored).
- [ ] A span straddling two chunks resolves to **both** stable_ids (the recall scorer's existing multi-gold partial credit handles this — `recall_at_k` already divides by `|gold|`).
- [ ] **Zero-resolve is a hard error**: an anchor that matches no current chunk raises and fails the run (with the offending question id + anchor text), never degrading to a silent `recall=0`. This is the load-bearing assertion of the story.
- [ ] Editing the *document content* so the quoted span no longer appears correctly breaks the anchor (zero-resolve → fail-loud); the resolver does not fuzzy-match around a content edit.
- [ ] After a `chunk_size`/overlap re-seed, re-running the eval requires **no** change to the golden set (the anchor re-resolves to the new chunk indices).
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A small corpus + a golden set with one anchor whose span lives in one chunk and one anchor whose span straddles two chunks.
- **Steps:** 1. Seed at 500/50, resolve, assert both anchors resolve (straddling → 2 stable_ids). 2. Re-seed at a different chunk_size, re-resolve with the *unchanged* golden set, assert resolution still succeeds. 3. Introduce an anchor whose text appears in no chunk, run, assert the run raises a clear zero-resolve error and exits non-zero.
- **Expected Result:** Anchors resolve across chunking sweeps with zero re-labeling; a straddling span yields both chunks; a non-matching anchor fails loud.
- **Failure Indicator:** A re-chunk silently re-points or drops a label, a straddling span resolves to one chunk, or a zero-resolve anchor yields a silent `recall=0` instead of a hard error.

### US-108: Layered golden-set format — base + derived-for-free + support-face label
**Description:** As a buyer, I want one layered golden set where labeling gold chunks once auto-generates the E4 viewer matrix and E7 P1b population, and I add an escalation label per question only if I ship the support face, so that authoring burden scales with the faces I ship and the permission matrix can never drift from the retrieval gold.
**Acceptance Criteria:**
- [ ] **Base layer (every buyer):** `question → gold content-anchor labels` (US-107) + `category`. The minimal primitive.
- [ ] **Derived for free (zero extra authoring):** the runner constructs the three viewer setups deterministically from the gold labels — `full_access` (owner), `partial_access = gold ∪ N filler`, `no_access = all_non_gold` — exactly today's `viewer_construction` rule in `retrieval_gold.yaml` + `runner.py::compute_visible_stable_ids`; and the E7 **P1b** population = the same question under a `no_access` viewer. The buyer hand-writes neither a permission test nor a P1b case.
- [ ] **Support-face layer (only support buyers):** one `escalation` label per question ∈ `{no_context (P1a), answerable_faithful (P2), should_escalate (P3)}`. P2-vs-P3 needs a human "does a faithful answer exist from these chunks?" judgment and so cannot be derived; a knowledge-assistant-only buyer never authors it (the loader treats the support layer as optional and absent for non-support buyers).
- [ ] A non-support golden set (no `escalation` labels) loads and runs the base + derived-for-free layers without error; a support golden set additionally runs the escalation suite.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** Two golden sets — one base-only, one with `escalation` labels.
- **Steps:** 1. Load the base-only set, run the eval, assert the three viewer setups and the P1b population were derived without any hand-authored permission/P1b entries. 2. Load the support set, assert the escalation suite additionally runs and reads the per-question label. 3. Confirm the base-only set does not error on the absent support layer.
- **Expected Result:** Labeling gold once yields the security/leak matrix and P1b for free; the escalation label is the only support-only addition.
- **Failure Indicator:** A buyer must hand-author the viewer matrix or P1b cases, or a non-support golden set fails because it lacks escalation labels.

### US-109: Golden-set authoring guide — teach the completeness contract + single-family-weaker caveat
**Description:** As a buyer authoring my own golden set, I want a guide that teaches exhaustive gold labeling as the correctness contract and states plainly that single-family evals are a weaker proof so that I do not manufacture a false security pass by under-labeling or cite a lenient single-family score as "proven."
**Acceptance Criteria:**
- [ ] The guide teaches the **completeness contract**: because `no_access = all_non_gold` and `partial = gold ∪ N filler`, an under-labeled relevant chunk lands in the filler pool and can produce a **false security pass the green checkmark won't reveal** — so exhaustive gold labeling is load-bearing, not merely a recall concern. (Distinct from the re-chunking brittleness, which content anchors solve.)
- [ ] The guide documents content anchoring (US-107): author answer-bearing text, not chunk indices; zero-resolve fails loud; editing source content breaks the anchor by design.
- [ ] The guide **actively recommends** cross-family corroboration in moat terms (one extra weekly judge pass, cents, turns "a number moved" into "two independent judges agree") and states the optional binding mechanics (US-103).
- [ ] The guide states **loudly** that **single-family evals carry same-family bias and are a weaker proof** than the cross-family configuration the kit demonstrates — a buyer must not cite a lenient single-family faithfulness score to a client as "proven." Cross-references the F3 row.
- [ ] The guide names the support-face escalation label as the only support-only authoring step (US-108).
- [ ] Typecheck/lint passes (docs-only)
**Validation Test:**
- **Setup:** Open the authoring guide.
- **Steps:** 1. Confirm a section teaches under-labeling → false security pass, with the `all_non_gold` / `gold ∪ filler` mechanism. 2. Confirm the single-family-weaker caveat is stated as a non-citable-as-proven warning. 3. Confirm content anchoring + fail-loud and the cross-family recommendation are present.
- **Expected Result:** The guide teaches completeness as the correctness contract and the single-family bias caveat unambiguously.
- **Failure Indicator:** Under-labeling is framed only as a recall issue, or single-family evals are presented as equivalent proof to cross-family.

### US-110: Generic corpus seeder — corpus only, optional manifest, never bakes eval scaffolding
**Description:** As a buyer, I want the seeder to seed a corpus and nothing eval-specific — point it at a folder of docs plus an optional manifest of workspaces / principals / *real* grants → chunk → embed → index — so that I can run it against my **production** corpus without polluting it with synthetic test principals.
**Acceptance Criteria:**
- [ ] The genericized seeder (lifted from `db_seed/corpus_seed.py`) reads a docs folder + an **optional** manifest describing real workspaces / principals / grants, then runs the production `chunk_text` + `embed_texts` paths (unchanged — the eval must measure the real code path) and inserts `documents` + `chunks` (+ real `chunk_acl` / membership rows from the manifest).
- [ ] The seeder **never** inserts the synthetic eval viewers (`PARTIAL_VIEWER_ID` / `NO_ACCESS_VIEWER_ID`) or the derived `full/partial/no_access` ACL matrix — those are constructed by the **runner at run time** from gold labels (`runner.py::ensure_viewer_users` / `reset_viewer_acls` stay runner-side), so a production seed carries zero test principals.
- [ ] Running the seeder with no manifest seeds an owner-only corpus (empty `chunk_acl` → owner-only behavior, consistent with the no-backfill rollout).
- [ ] The seeder remains re-runnable / idempotent (purge-by-metadata + re-insert) and preserves byte-stable `(stable_id, content)` pairs across re-seeds.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A docs folder + a manifest granting one real principal one document.
- **Steps:** 1. Seed against a fresh DB. 2. Query `chunk_acl` for the synthetic eval-viewer UUIDs → assert zero rows. 3. Assert the manifest's real grant produced its `chunk_acl` rows. 4. Run the eval runner and confirm it constructs the synthetic viewers transiently at run time. 5. Re-seed and confirm `(stable_id, md5(content))` pairs are unchanged.
- **Expected Result:** The seed contains only real corpus + real grants; synthetic eval scaffolding exists only during a runner invocation.
- **Failure Indicator:** A seeded DB contains synthetic eval principals or a derived ACL matrix, or a no-manifest seed is anything but owner-only.

### US-111: Ship green out of the box — default e-commerce corpus + content-anchored golden set
**Description:** As a buyer, I want a fresh `seed → eval` to produce the 1.000 no-leak table on the first run so that the kit's day-zero "it works" moment (the P3 build-in-public demo) needs no authoring before the first eval can run.
**Acceptance Criteria:**
- [ ] The default corpus is the existing 7-doc / 14-chunk e-commerce set in `db_seed/corpus/`; its golden set is authored in the **content-anchor** format (US-107) so the example anchors resolve against the shipped corpus.
- [ ] A clean `seed → eval` on the default corpus reproduces the 1.000 no-leak security table (E4: `no_access` retrieves 0 gold under both filters) on the first run, with no buyer authoring.
- [ ] The default `gate.yaml` (US-103) reproduces today's gate constants so the shipped configuration is green.
- [ ] The demo corpus + its golden set are maintained artifacts that stay green in the **kit's own CI** (a dead demo corpus embarrasses the quickstart) — a CI job runs `seed → eval` on the default corpus and asserts the green table.
- [ ] Typecheck/lint passes
**Validation Test:**
- **Setup:** A clean DB and the default corpus + shipped golden set, no buyer edits.
- **Steps:** 1. Run the seeder, then the eval runner. 2. Read the `security_no_access` aggregate. 3. Confirm the no-leak table is 1.000 / 0 gold across `no_access` cells. 4. Confirm the kit-CI green-run job passes.
- **Expected Result:** Day-zero `seed → eval` is green; the kit's own CI keeps the demo green.
- **Failure Indicator:** The first run requires authoring, the no-leak table is not 1.000, or the demo corpus is not exercised in kit CI.

### US-112: Demo-corpora worked-examples doc (e-commerce / Wikipedia / CRM) + swap honesty
**Description:** As a buyer choosing how to model my own data, I want a doc presenting the three demo corpora as role-specific worked examples — not interchangeable defaults — and stating plainly that swapping in my own corpus makes the example anchors fail loud, so that I understand "replace the corpus" and "author a new golden set" are the same step.
**Acceptance Criteria:**
- [ ] The doc presents three role-specific examples: **e-commerce** (default — permissions + escalation, small/fast/relatable), **Wikipedia 10k** (scale-benchmark *filler only*, never golden-answerable — golden questions stay anchored to the real docs), **CRM** (text-to-SQL optional-module example, X1).
- [ ] The doc states the **honest framing** (a direct consequence of content anchoring, US-107): swapping in the buyer's corpus makes the example golden set's anchors **fail loud** — the example set is a **format template to learn from, not a survives-the-swap artifact**; the guide must not imply the example questions will work on the buyer's docs.
- [ ] The doc cross-references US-110 (seeder = corpus only) and US-108/US-109 (author a new golden set on swap).
- [ ] Typecheck/lint passes (docs-only)
**Validation Test:**
- **Setup:** Open the demo-corpora doc.
- **Steps:** 1. Confirm each corpus is labeled with its role and that Wikipedia is marked filler-only / never-gold. 2. Confirm the swap-fail-loud framing and "replace corpus = author new golden set" statement are present.
- **Expected Result:** The doc frames the three corpora as worked examples and is honest that example anchors do not survive a corpus swap.
- **Failure Indicator:** The corpora are presented as interchangeable defaults, or the doc implies the example golden set survives a buyer corpus swap.

#### Functional requirements (this area)
- FR-E1: Every eval output resolves to exactly one gate class (`security` | `quality`), with `false_resolve` marked as a quality metric whose ceiling is a pinned invariant (US-101).
- FR-E2: Security/correctness invariants (E4 / E6 / AU4 / E7-P1b) are pinned `fail`, not buyer-downgradable; the only removal path is deleting the eval (US-102).
- FR-E3: The detection layer in `ragas_gates.py` is kept wholesale; only its bindings (cells, `CLAUDE_EQUIVALENT`, `CLAUDE_JUDGE_CELL`, threshold constants) move into a buyer-authored gate declaration, and the default declaration reproduces today's constants byte-for-byte (US-103).
- FR-E4: The verdict layer is a per-suite `off|comment|fail` loudness knob over the existing `red`/`yellow` severity (`fail` ⇒ red fails / yellow comments; `comment` ⇒ both comment; `off` ⇒ silent; default `comment`), not a new detector (US-104).
- FR-E5: A determinism axis decides merge-blocking: deterministic gates may `fail`-per-PR ⇒ block merge; non-deterministic (LLM-judged) gates are scheduled-only, `fail` ⇒ fail workflow + file issue (US-105); the false-resolve faithfulness-leg latency gap is documented (US-106).
- FR-E6: Gold labels are content anchors resolved to current chunks at eval time; a straddling span resolves to both; **zero-resolve is a hard error, never a silent `recall=0`** (US-107).
- FR-E7: One layered golden set — base (`question → gold anchors` + category), derived-for-free (E4 viewer matrix + E7 P1b), support-face (one escalation label) — with completeness taught as the correctness contract and single-family framed as weaker proof (US-108, US-109).
- FR-E8: The seeder seeds a corpus (+ optional real-grant manifest) and nothing eval-specific; synthetic viewers and the derived ACL matrix are built transiently by the runner, never baked into a (production) seed (US-110).
- FR-E9: The kit ships green: default e-commerce corpus + content-anchored golden set produce the 1.000 no-leak table on first `seed → eval` and stay green in the kit's own CI (US-111); the three demo corpora are documented as role-specific worked examples with fail-loud swap honesty (US-112).

#### Non-goals (this area)
- Flattening the detection layer to fixed per-metric thresholds (drops rolling-window / cross-family corroboration — the moat's credibility).
- A single uniform `off|comment|fail` knob over all suites including security/correctness (PRD A5 literal — would let a leak merge green).
- Per-PR LLM-judged `fail` (a judge wobble red-barring an innocent merge); a blocking per-PR LLM-faithfulness gate to close the false-resolve latency gap.
- Chunk-index gold labels + a "re-label after re-chunking" step (kills the eval-driven iteration loop the product sells).
- Baking synthetic eval principals / the derived ACL matrix into the seed (pollutes a production corpus).
- Requiring cross-family corroboration (a hard second-vendor dependency fighting the 30-min quickstart) — it is recommended-but-optional, with a loud single-family-weaker caveat.
- Separate hand-authored golden sets per suite (more buyer work; the permission matrix can drift from the retrieval gold).
- Shipping empty (no default golden set / demo corpus) — the day-zero green run is the most persuasive buyer experience.
---

## Cross-cutting technical considerations

### Shared constants — reconcile before the first migration

- **Default Workspace UUID** is a single canonical constant. §A (US-002) pins one and backfills *all* existing documents and users — including the synthetic eval viewers (`PARTIAL_VIEWER_ID` / `NO_ACCESS_VIEWER_ID` in `evals/retrieval/runner.py`) and the corpus sentinel user — into it, so the existing correctness eval (E4) stays bit-for-bit green. Every later reference (seeds, E6, E7, support-bot provisioning) MUST use that same constant.
- **Support-bot principal:** one per workspace, `role='member'` + `is_bot` flag, provisioned **lazily** (§E) when support is first enabled — a `workspace_membership` row in a §A workspace. E7 (§D) and the widget retrieval path (§E) both reference it; it sees only what *share-to-bot* grants via `chunk_acl`.

### Known traps (surfaced during drafting — call these out in review)

1. **Subtractive workspace RLS must conjoin (AND), never add an OR policy** (§A US-005). Module 11's `chunk_acl` mirror was *additive* (OR-ed to widen); the workspace boundary is *subtractive* (it hides). A new OR-policy silently re-opens isolation — each owner/ACL branch must *additionally* require membership.
2. **Surface the pre-fusion raw cosine before any escalation gate** (§D US-046). Hybrid fusion currently overwrites `similarity` with the RRF rank artifact, which is not comparable across queries; the retrieval gate thresholds raw cosine `[0,1]`, so the cosine must be plumbed through first. This is the load-bearing prerequisite for every gate story.
3. **Bot token vs customer token** (§E). The bot token *can* retrieve → it is server-side-only, minted per-turn, never shipped. Only the **opaque** customer token (cannot retrieve, conversation-scoped) reaches the iframe. Conflating them is a leak.
4. **`match_chunks` keeps its signature/return shape** under the workspace clause (DROP-and-CREATE, identical GRANT) — no generated-client/type churn (§A).

### F3 capability-matrix rows owed (consolidate when F3 is built)

Per CONTEXT's "F3 is a standing sink for accepted gaps" discipline, these sections each owe rows; F3 owns the canonical table, sections supply rows:
- **B (model):** Responses-mode OpenAI-only; other compatible endpoints untested; native non-OpenAI out; Entra/AAD deferred.
- **C (ingestion):** OCR not in the default parser; markdown-string output ceiling; Unstructured is BYO-adapter.
- **D/F (escalation/eval):** single-family judge is a weaker proof; safety-critical faithfulness regressions have up-to-a-week detection latency.
- **E (support):** widget-closed-before-reply reached manually or not at all; fully-deflected conversations accumulate in `active`; workspace members can read customer-pasted PII; no cross-device customer identity; origin allowlist is defense-in-depth only; the backend now holds the JWT signing secret (also a P5 threat-model line).

## Consolidated non-goals (global)

Per-area non-goals are inline at each section's end. Globally out for this PRD: **S5 teams / assignment routing**, **P1b access-aware detection** (no privileged second pass in code), **S6 BYO-key / per-workspace trace RLS**, **S7 billing**, **Observability O1–O4 + F2 + F3 as build work** (follow-up PRD), VAPI/voice, OCR in the default parser, native non-OpenAI runtime APIs, nested groups, MFA/SCIM/magic links, self-serve workspace creation.

## Success metrics

- **Zero leakage, two axes:** E4 (per-viewer) and E6 (per-workspace) both report `0` gold-chunk leakage; both are pinned-`fail` and cannot be configured down.
- **AU4:** forged / missing / expired / cross-workspace JWT → `0` rows on every endpoint; per-conversation token reads only its own conversation; customer token retrieves `0` chunks.
- **Deflection:** E7 reports deflection rate maximized subject to false-resolve ≤ the buyer-set ceiling; breaching the ceiling fails the build.
- **Non-disclosure:** customer-facing P1b output is byte-for-byte identical to P1a (pinned-`fail`).
- **Portability:** OpenAI *and* Azure pass the model-surface tests; an embedder model/dim mismatch refuses to start; the LlamaParse adapter round-trips a real fixture behind the unchanged `DocumentParser` contract.
- **Day-zero:** a fresh `seed → eval` on the default e-commerce corpus produces the 1.000 no-leak table on the first run.

## Open questions

1. **SSE fan-out backplane.** The customer reply channel (§E) uses Postgres `LISTEN/NOTIFY` for multi-instance fan-out. Confirm the per-client deploy target is single-instance for v1; if not, the backplane is load-bearing and needs its own story.
2. **Default Workspace UUID** — confirm one canonical value across §A, the seeder (§F), and bot provisioning (§E) before the first migration runs.
3. **Global-only knobs in v1** — confirm per-workspace escalation tuning and per-workspace trace RLS stay deferred (config-shaped, S6).
4. **Follow-up PRD** — schedule Observability (O1–O4), F2 typed client, and F3 matrix; they gate the LangSmith-contradiction cleanup and the P3/P5 marketing artifacts.
