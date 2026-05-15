-- US-037: principal_membership maps users into groups.
--
-- principal_id is intentionally not a FK to public.principals: the column is
-- also used for principal_type='user' grants in chunk_acl where principal_id
-- is an auth.users.id. Keeping membership FK-free lets the same UUID column
-- carry either kind without per-kind branching elsewhere. The chunk_acl RLS
-- and match_chunks predicate only look at this table for group resolution
-- (rows with principal_id = a group UUID), so user-direct grants bypass it.
--
-- RLS: callers can only see their own memberships. This enforcement matters
-- because match_chunks runs SECURITY INVOKER and resolves the viewer's group
-- set via this table — RLS on the subselect prevents one user from
-- impersonating another's group set even if they could craft a query.

create table public.principal_membership (
  principal_id uuid not null,
  member_user_id uuid not null references auth.users(id) on delete cascade,
  primary key (principal_id, member_user_id)
);

create index principal_membership_member_user_id_idx
  on public.principal_membership (member_user_id);

alter table public.principal_membership enable row level security;

create policy principal_membership_select_own on public.principal_membership
  for select using (member_user_id = auth.uid());
