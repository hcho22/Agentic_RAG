# ADR 0002: Workspace tenant isolation layered above owner-OR-ACL

- **Status:** Accepted
- **Date:** 2026-06-15

## Context

The built permission model (Module 11) is single-namespace: a chunk is visible
to a viewer iff they are the **owner** (`chunks.user_id = auth.uid()`) or a
principal in their resolved set holds a `chunk_acl` grant — resolved inside
`match_chunks` (`SECURITY INVOKER`) from `auth.uid()`, with mirrored RLS on
`chunks`/`documents` for defense in depth. `CONTEXT.md` records the known gap:
"single-namespace in v0 (no workspace/tenant scoping) — a known caveat for
production credibility."

Phase 2 (PRD R6/R7) closes that gap with a **Workspace** — a hard tenant
boundary above the existing model. The product hero becomes "document-level
sharing *and* org-level isolation, both enforced in the retrieval predicate,
both eval-proven." This required deciding *where* the boundary is enforced, how
existing data migrates, and how the boundary relates to the existing Principal
and (deferred) role concepts — without cracking the "trust boundary is the
database" property that the leak/correctness eval already proves.

## Decision

Add a Workspace as a hard isolation boundary enforced by **membership**, not by
a backend-supplied tenant ID.

- **Membership is the security boundary, enforced in the DB.** A chunk is
  visible iff `(owner OR ACL grant) AND viewer ∈ members(document.workspace_id)`.
  The membership clause lives *inside* `match_chunks` and is mirrored in the
  `chunks`/`documents` RLS — an EXISTS subquery against `workspace_membership`
  keyed on `auth.uid()`. The backend never passes a workspace ID as the
  boundary. A forgotten backend filter can only *widen* to workspaces the viewer
  already belongs to; it can never leak across the boundary. This is the clause
  the E6 eval tests.
- **"Active workspace" is a non-security UX filter**, carried in the API
  path/context (`/api/workspaces/{id}/...`) and validated against the caller's
  memberships — *default-when-sole*, *400-on-ambiguous*. It scopes which
  workspace a query/upload acts in; it is deliberately *not* trust-load-bearing,
  because the membership clause already prevents cross-workspace leakage.
- **User ↔ Workspace is many-to-many in the schema, single-active in the v1 UX.**
  A `workspace_membership(workspace_id, user_id, role)` join table ships now (a
  brutal retrofit later for the support face); the v1 reference UI never shows a
  selector.
- **`role ∈ {admin, member}` is administrative only.** It governs member/group
  management and grants **no** content access — it never appears in any
  retrieval predicate. This preserves the glossary's deferral of *access-control*
  (RBAC permission-bundle) roles; the new role is a different axis.
- **Migration via a single Default Workspace.** The boundary is *subtractive*
  (it can hide a document even from its owner), so unlike `chunk_acl` there is no
  safe "do nothing" default. All existing documents and users (including the
  synthetic eval viewers) land in one Default Workspace, making the membership
  clause a no-op for the legacy corpus. `documents.workspace_id` is not-null;
  `principals` become workspace-scoped (`(workspace_id, name)` unique,
  membership-gated RLS).

## Consequences

- The existing correctness eval (E4) passes **bit-for-bit unchanged** — the
  Default Workspace makes membership inert for the legacy corpus. E6 is
  *additive*: a second Workspace proves zero gold-chunk leakage across the
  partition, reusing the same harness.
- The security narrative is preserved and extended: the tenant boundary is
  enforced in the same `SECURITY INVOKER` function, resolved from the same
  `auth.uid()`, proven by the same eval infra. No new trust surface.
- `principals` RLS tightens from `using (true)` to membership-gated, closing the
  group-catalog enumeration leak that was acceptable only in the single-namespace
  world. The share dialog's email-resolution becomes workspace-bounded.
- Load-bearing index `workspace_membership(user_id, workspace_id)` so the
  membership EXISTS is index-served per viewer. `workspace_id` lives on
  `documents` only (not denormalized onto `chunks`), since `match_chunks`
  already joins `documents`.
- API-layer auth attack tests (AU4) gain a cross-workspace JWT case: a valid
  token for workspace A must retrieve zero rows from workspace B on every
  endpoint.

## Alternatives considered and rejected

- **Inherit orgs from Clerk (ADR-0006).** Rejected by A2 — Supabase Auth is woven
  through every RLS policy; migrating the IdP means rewriting the security core.
  Workspaces are built in-schema instead.
- **Backend-passed `workspace_id` as the boundary.** Rejected — it moves the
  tenant boundary out of the database and undoes the "trust boundary is the DB"
  property that the leak eval proves.
- **Per-owner personal workspaces on migration.** Rejected — it breaks existing
  cross-user `chunk_acl` shares (the partial-access viewer cases in E4) and would
  force a same-day rewrite of the eval's viewer setups.
- **Stateful `users.active_workspace_id` pointer.** Rejected for path-based
  resolution — a pointer introduces hidden global state the widget and typed
  client can desync against; the path is explicit and per-request.
- **Admin role implies content read across the workspace.** Rejected — it would
  revive the deferred access-control RBAC through the back door and punch a hole
  in owner-OR-ACL. An admin who must read everything is *granted* it via the ACL.

## Identity Boundary (AU3)

US-011 pins what an integrator may swap in the auth stack and what is welded to
the security core. This is the formal record of the CONTEXT "Identity boundary
(AU3)" design note; it lives under this ADR because the boundary is the very same
`auth.uid()` the workspace-membership clause (US-003) resolves against — swapping
identity wrong is just another way to cross the tenant boundary.

- **The floor (non-negotiable): the data plane is a Supabase-JWT pass-through.**
  `get_user` (`backend/main.py:361`) validates the bearer token against GoTrue
  (`GET {SUPABASE_URL}/auth/v1/user`); every data call then forwards *the user's
  own JWT* to PostgREST via `_supabase_headers` (`backend/main.py:390`). So
  `auth.uid()` inside `match_chunks`, `keyword_search`, and the `chunks` /
  `documents` SELECT RLS — **including the new workspace-membership clause**
  (US-003 / US-004 / US-005) — is resolved **by Postgres, from the Supabase JWT**.
  The backend never passes a principal ID or a workspace ID as the access
  boundary (the active-workspace path value is a non-security UX filter, validated
  against membership, never the retrieval predicate). The trust boundary therefore
  *requires* "a valid Supabase JWT whose `sub` is an `auth.users` row," and AU4
  (US-010) proves a forged / missing / expired / cross-workspace / tampered-`sub`
  JWT retrieves **zero rows on every retrieval endpoint**.

- **The contract (what may be swapped): _verified external identity → a Supabase
  session whose `sub` is an `auth.users` row._** The supported v1 swap is
  **federating the client's IdP into Supabase Auth** — native SAML SSO, external
  OIDC, or social providers — so the client's existing identity provider becomes
  the *authenticator* while the data plane (PostgREST + RLS + `match_chunks`) is
  **unchanged**, because it still sees an ordinary Supabase JWT. Buyer framing:
  **swap who authenticates, not the principal store.**

- **Out of scope (would rewrite the security core): the `auth.users` UUID floor
  cannot move.** Full **principal-store replacement** — a backend-passed principal
  with RLS rewritten off `auth.uid()` — is **rejected**: it moves the
  tenant/permission boundary out of the database and deletes the "trust boundary
  is the DB" property the leak/correctness eval (E4/E6) proves. A client demanding
  Supabase Auth be ripped out entirely is a security-core rewrite, **not a v1
  configuration** (consistent with the A2 rejection of inheriting orgs from an
  external IdP, above).

- **Future seam (documented, not v1): a backend "JWT-exchange" adapter** — verify
  a *foreign* JWT, then mint a Supabase session via the admin API — for clients
  who cannot use Supabase's native SSO. It keeps `auth.uid()` intact but adds a
  token-minting + admin-API surface that needs its own audit, so it is recorded as
  a future seam, **not shipped in v1.**

**Capability matrix (F3) row.** _Identity provider — swappable at the federation
edge only: federate the client IdP into Supabase Auth (SAML / OIDC / social). The
`auth.users` principal store is fixed; full principal-store replacement is out of
scope; a foreign-JWT exchange adapter is a documented future seam, not v1._
(Cross-ref: CONTEXT "Capability matrix (F3)".)

**Threat model (P5) line.** _The Identity Boundary is the Supabase JWT: every
retrieval path resolves `auth.uid()` from the user's forwarded token inside the
database, so the auth floor (`get_user` → GoTrue) plus the membership/ACL
predicate are the whole boundary. Federating an external IdP changes only who
issues the upstream identity — it does not widen what a token can read. The
future JWT-exchange adapter would add a token-minting surface requiring its own
audit._ (Cross-ref: CONTEXT "Identity boundary (AU3)".)
