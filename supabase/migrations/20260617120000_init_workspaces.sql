-- US-001 (ADR-0002): a Workspace is the hard tenant boundary layered ABOVE the
-- existing owner-OR-ACL model. A chunk becomes visible iff
--   (owner OR ACL grant) AND viewer ∈ members(document.workspace_id)
-- with the membership clause enforced inside match_chunks / keyword_search and
-- mirrored in the chunks/documents RLS (later stories in this area). This
-- migration introduces only the workspaces registry; the load-bearing
-- workspace_membership join — the actual access surface — follows in the
-- companion migration 20260617120100_init_workspace_membership.sql.
--
-- Workspace creation is operator-level in v1 (seed script / admin endpoint),
-- never self-serve (ADR-0002: self-serve creation + billing is a non-goal), so
-- no INSERT policy is granted to authenticated callers. RLS is enabled now so
-- the table is deny-by-default via PostgREST (consistent with every other table
-- in this schema). A member-scoped SELECT policy is intentionally deferred to
-- the active-workspace / membership-admin stories (US-007 / US-008) that first
-- need to read this table; retrieval never reads workspaces directly (it joins
-- documents + workspace_membership), so deny-by-default does not affect the
-- retrieval path.

create table public.workspaces (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now()
);

alter table public.workspaces enable row level security;
