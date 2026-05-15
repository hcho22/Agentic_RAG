-- US-037: profiles mirrors auth.users(id, email) so the share dialog can
-- resolve a typed email to a user UUID without giving the frontend access
-- to the auth schema.
--
-- The trigger keeps the mirror in sync on every insert/update of auth.users.
-- A backfill seeds existing rows so the mirror is correct immediately after
-- the migration runs (otherwise users created before this migration would be
-- invisible to email lookups until they next updated their auth row).
--
-- RLS: select-true is intentional — any authenticated user must be able to
-- look up another user by email to share a document with them. The fact that
-- the email exists is not itself sensitive in this product (single-tenant
-- demo); production deployments would want a SECURITY DEFINER lookup RPC
-- instead, but that's out of scope for v0.

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text unique not null
);

alter table public.profiles enable row level security;

create policy profiles_select_all on public.profiles
  for select using (true);

create or replace function public._sync_profile_from_auth()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.email is null then
    return new;
  end if;
  insert into public.profiles (id, email)
    values (new.id, new.email)
    on conflict (id) do update set email = excluded.email;
  return new;
end;
$$;

create trigger sync_profile_from_auth
  after insert or update of email on auth.users
  for each row execute function public._sync_profile_from_auth();

-- Backfill so existing users are immediately resolvable by the share dialog.
insert into public.profiles (id, email)
  select id, email from auth.users where email is not null
  on conflict (id) do nothing;
