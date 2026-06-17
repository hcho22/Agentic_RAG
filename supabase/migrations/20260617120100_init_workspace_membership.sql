-- US-001 (ADR-0002): workspace_membership is the load-bearing tenant-isolation
-- surface — the join that decides which viewers can see a workspace's documents.
-- User ↔ Workspace is modeled MANY-TO-MANY from day one (single-active in the v1
-- UX) so the support face's cross-workspace operators do not force a brutal
-- retrofit later.
--
-- The membership clause resolves the boundary from auth.uid() INSIDE
-- match_chunks / keyword_search (added in later stories): an EXISTS against this
-- table keyed on (user_id, workspace_id). The (user_id, workspace_id) index
-- below is the load-bearing one — it serves that per-viewer EXISTS index-only,
-- mirroring the chunk_acl (principal_id, chunk_id) precedent in
-- 20260514130300_permissions_chunk_acl.sql. The composite primary key
-- (workspace_id, user_id) separately serves admin listing of a workspace's
-- members and the ON DELETE CASCADE path from workspaces.
--
-- role ∈ {admin, member} is ADMINISTRATIVE ONLY: it governs member/group
-- management and grants NO content access, so it must never appear in any
-- retrieval predicate (ADR-0002). It is a different axis from the deferred
-- access-control (RBAC) role concept; an admin who must read content is granted
-- it via the ACL like anyone else.
--
-- RLS: a caller sees only their own membership rows (user_id = auth.uid()),
-- mirroring principal_membership_select_own in
-- 20260514130100_permissions_principal_membership.sql. match_chunks runs
-- SECURITY INVOKER and resolves the viewer's workspace set via this table, so
-- this RLS prevents one user from reading (or inferring) another's workspace set.

create table public.workspace_membership (
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'member' check (role in ('admin', 'member')),
  created_at timestamptz not null default now(),
  primary key (workspace_id, user_id)
);

create index workspace_membership_user_id_workspace_id_idx
  on public.workspace_membership (user_id, workspace_id);

alter table public.workspace_membership enable row level security;

create policy workspace_membership_select_own on public.workspace_membership
  for select using (user_id = auth.uid());
