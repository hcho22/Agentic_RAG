-- SEC-F1 (audit data/purvia-rls-audit/report.md §3): close the cross-tenant
-- email enumeration on public.profiles WITHOUT breaking the legitimate,
-- relationship-scoped reads the product actually needs.
--
-- BEFORE: `profiles_select_all ... using (true)` (20260514130200) let ANY
-- authenticated user in ANY workspace read EVERY user's (id, email) across all
-- tenants via a direct PostgREST select — a cross-tenant PII disclosure.
--
-- The profiles mirror is read under the CALLER's JWT in exactly three legitimate,
-- relationship-scoped ways, none of which needs blanket enumeration:
--   (a) YOUR OWN email — match_chunks / keyword_search project the granting
--       principal's display for a user-grant as `select p.email from profiles
--       where p.id = auth.uid()` (SECURITY INVOKER; the grantee is the caller);
--   (b) the emails of users you SHARED YOUR OWN DOCUMENT with — `list_doc_shares`
--       (backend/permissions.py) resolves the grantees of a doc the caller owns;
--   (c) the email of a support-queue CLAIMER in one of your workspaces — the
--       support queue labels "Claimed by <email>".
--
-- FIX (a TIGHTENING; RLS stays on): drop the blanket read and replace it with a
-- SELECT policy scoped to (a)+(b) — you may read a profile row iff it is YOURS or
-- it belongs to a user you granted one of your own documents to. That is exactly
-- what match_chunks/keyword_search and list_doc_shares require, so THEY NEED NO
-- CODE CHANGE, while a caller can no longer read the directory: `GET
-- /rest/v1/profiles` now returns only self + your grantees, never another tenant's
-- users. Case (c) is a different relationship (claimer, not grantee) and the
-- share-dialog's exact-email lookup targets a NOT-YET-grantee, so both are served
-- by narrow SECURITY DEFINER RPCs below rather than by widening the policy.
--
-- The policy predicate is wrapped in a SECURITY DEFINER helper for the SAME reason
-- the chunks/documents/chunk_acl policies are (20260514130300 / 20260617120500):
-- it reads chunk_acl + chunks + documents, and running that under the querying
-- role would re-enter those tables' RLS (recursion / needless cost). The helper
-- bypasses RLS on the inner read but re-derives the caller from auth.uid()
-- INSIDE, so the DEFINER privilege never widens the boundary — it only answers
-- "is this profile visible to the caller". Its argument is the profile row's own
-- id (the policy passes `id`), so it is not a cross-user oracle: a direct call can
-- only reveal the CALLER's own sharing relationships, never a third party's.

-- 1. Remove the blanket read.
drop policy if exists profiles_select_all on public.profiles;

-- 2. Visibility helper: self OR a user-grantee on one of the caller's documents.
create or replace function public._profile_visible_to_caller(p_profile_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select p_profile_id = auth.uid()
    or exists (
      select 1
      from public.chunk_acl ca
      join public.chunks c on c.id = ca.chunk_id
      join public.documents d on d.id = c.document_id
      where ca.principal_type = 'user'
        and ca.principal_id = p_profile_id
        and d.user_id = auth.uid()
    );
$$;

create policy profiles_select_self_or_grantee on public.profiles
  for select using (public._profile_visible_to_caller(id));

-- 3. Exact-email resolution for the share dialog (replaces the backend's
--    `GET /rest/v1/profiles?email=eq.<x>&limit=1`, backend/main.py::_resolve_principal).
--
--    The share target is a user you have NOT yet granted to, so the policy above
--    would not expose them; this RPC resolves an EXACT address to its id (or
--    nothing). A miss returns zero rows — which the share flow already surfaces as
--    a 404 — so this reveals only "does this exact address exist", the minimum a
--    share-by-email feature needs, and never lets a caller enumerate the directory.
--
--    DEFINER (reads past RLS) but granted to `authenticated` ONLY: revoke the
--    Supabase-default execute from public/anon first (they are granted DIRECTLY,
--    not via PUBLIC — same footgun the US-071 / US-075 RPCs guard against), then
--    grant authenticated.
create or replace function public.resolve_profile_by_email(p_email text)
returns table (id uuid, email text)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select p.id, p.email
  from public.profiles p
  where p.email = p_email
  limit 1;
$$;

revoke execute on function public.resolve_profile_by_email(text) from public, anon;
grant execute on function public.resolve_profile_by_email(text) to authenticated;

-- 4. Claimer-label resolution for the support queue (replaces the frontend's
--    `.from('profiles').select('id,email').in('id', ids)`,
--    frontend/src/lib/supportQueue.ts::resolveClaimerEmails).
--
--    A claimer is an agent who claimed a conversation — NOT a grantee of the
--    caller's documents — so the policy above does not cover them; this RPC
--    resolves the given ids to emails ONLY for ids that are the `claimed_by` agent
--    on some conversation in a workspace the CALLER is a member of. So a caller can
--    put a name to a fellow agent in their own queue, and to nobody else: a foreign
--    uid (or one that has not claimed in the caller's workspaces) yields zero rows.
--
--    DEFINER (reads past RLS) but the membership predicate is re-derived from
--    auth.uid() INSIDE, so the DEFINER privilege never widens the boundary.
create or replace function public.resolve_claimer_emails(p_ids uuid[])
returns table (id uuid, email text)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select p.id, p.email
  from public.profiles p
  where p.id = any(p_ids)
    and exists (
      select 1
      from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.claimed_by = p.id
        and wm.user_id = auth.uid()
    );
$$;

revoke execute on function public.resolve_claimer_emails(uuid[]) from public, anon;
grant execute on function public.resolve_claimer_emails(uuid[]) to authenticated;
