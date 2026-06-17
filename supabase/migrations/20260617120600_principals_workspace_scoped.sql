-- US-006 (ADR-0002): make the principals (group) catalog workspace-local.
--
-- principals was designed single-namespace (one customer): `name` was GLOBALLY
-- unique (so only one "finance" group could ever exist) and its SELECT RLS was
-- `using (true)` (so any authenticated user could enumerate every group name in
-- the system). Both are multi-tenant defects once a second workspace exists:
--   1. collision — two client workspaces must each be able to name a "finance".
--   2. catalog enumeration leak — a workspace must not see another's group names.
--
-- This is LEAKAGE/COLLISION hygiene, NOT the load-bearing access control: the
-- content boundary is the US-003 membership clause inside match_chunks. Even a
-- mis-grant to an out-of-workspace group cannot leak content, because that clause
-- already blocks cross-workspace retrieval (ADR-0002 / CONTEXT "Workspace-scoped
-- Principals"). Scoping principals fixes collisions + stops leaking group NAMES.

-- 1. workspace_id — staged exactly like documents in US-002 (add nullable,
--    backfill, transitional default, then NOT NULL + FK), so the migration
--    applies on the existing Module-11 principals and any raw INSERT that does
--    not yet supply workspace_id lands in the Default Workspace rather than
--    failing the NOT NULL. Group creation is operator-level in v1 (no self-serve
--    flow), so the default is the transitional analogue of US-002's.
alter table public.principals
  add column workspace_id uuid;

update public.principals
   set workspace_id = '00000000-0000-0000-0000-0000000000d0'
 where workspace_id is null;

alter table public.principals
  alter column workspace_id set default '00000000-0000-0000-0000-0000000000d0',
  alter column workspace_id set not null;

alter table public.principals
  add constraint principals_workspace_id_fkey
  foreign key (workspace_id) references public.workspaces(id);

-- 2. Uniqueness moves from global (name) to per-workspace (workspace_id, name),
--    so each workspace owns its own group namespace. The new unique index also
--    serves workspace_id-prefix lookups (no separate index needed).
alter table public.principals
  drop constraint principals_name_key;

alter table public.principals
  add constraint principals_workspace_id_name_key unique (workspace_id, name);

-- 3. RLS tightens from `using (true)` to membership-gated: a caller sees a group
--    only if they are a member of that group's workspace. This closes the
--    group-catalog enumeration leak that was only acceptable single-namespace.
--    Because _resolve_principal (backend/main.py) reads principals under the
--    caller's JWT, out-of-workspace groups now resolve to nothing automatically
--    — no backend change required.
drop policy principals_select_all on public.principals;

create policy principals_select_member on public.principals
  for select using (
    exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = principals.workspace_id
        and wm.user_id = auth.uid()
    )
  );
