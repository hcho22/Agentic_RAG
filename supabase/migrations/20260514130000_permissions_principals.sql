-- US-037: principals registry for group-style ACL grants.
--
-- Module 11 introduces denormalized chunk_acl rows keyed by (principal_type,
-- principal_id). For principal_type='user', principal_id is an auth.users.id
-- directly. For principal_type='group', principal_id references a row in this
-- table — the group registry. v0 only supports flat groups (no nesting); the
-- check constraint pins kind='group' so future kinds (eg. 'role') require an
-- explicit migration that loosens the constraint.
--
-- RLS: any authenticated user can read group names so the share dialog can
-- resolve a typed group name to a UUID. Membership is gated by
-- principal_membership RLS, so leaking the group catalog is acceptable.

create table public.principals (
  id uuid primary key default gen_random_uuid(),
  name text unique not null,
  kind text not null default 'group' check (kind = 'group'),
  created_at timestamptz not null default now()
);

alter table public.principals enable row level security;

create policy principals_select_all on public.principals
  for select using (true);
