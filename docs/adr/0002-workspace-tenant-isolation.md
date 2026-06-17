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
