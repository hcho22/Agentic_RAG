-- SEC-F3 (audit data/purvia-rls-audit/report.md §3): remove the cross-user
-- boolean-oracle surface from the ACL RLS helper functions.
--
-- BEFORE: three SECURITY DEFINER helpers take an ARBITRARY caller-supplied
-- p_user_id and return a boolean:
--   _chunk_acl_grants_user(p_chunk_id, p_user_id)        (20260514130300)
--   _document_has_acl_grant_for_user(p_document_id, p_user_id)  (20260514130300)
--   _chunk_belongs_to_doc_owner(p_chunk_id, p_user_id)   (20260514140000)
-- Supabase grants EXECUTE on new public functions to anon/authenticated by
-- default and these carry no revoke, so the browser role can call them directly
-- as PostgREST RPCs (`POST /rest/v1/rpc/_chunk_acl_grants_user`) and probe
-- cross-user relationships — "does user Y hold a grant on chunk X", "does Y own
-- the doc behind chunk X" — a boolean oracle over other users' ACL/ownership
-- metadata. In the policies these helpers are ALWAYS invoked with auth.uid(), so
-- p_user_id is never legitimately anything but the caller.
--
-- FIX: drop the p_user_id parameter and read auth.uid() INSIDE each helper. A
-- blanket `revoke execute from authenticated` would be WRONG — these functions are
-- evaluated while checking the chunks/documents/chunk_acl SELECT/INSERT/DELETE
-- policies for the querying role, which needs EXECUTE for those legitimate reads —
-- so instead we make the arbitrary-user probe structurally impossible: after this
-- an attacker can only ever ask about THEIR OWN grants/ownership, which they are
-- already entitled to know. auth.uid() resolves from the request JWT even inside a
-- SECURITY DEFINER function (DEFINER changes the privilege/role, not the
-- request.jwt.claims GUC PostgREST sets), so the boundary is unchanged — only the
-- caller-supplied-identity attack surface is removed.
--
-- `_user_in_document_workspace(p_document_id)` (20260617120500) already has this
-- safe single-arg / auth.uid()-internal shape and is intentionally left untouched.
--
-- ORDER (each step is a prerequisite of the next):
--   1. create the single-arg overloads (both signatures coexist during the swap),
--   2. repoint every policy call site to the single-arg form,
--   3. drop the now-unreferenced two-arg helpers (dependents first).

-- 1. Single-arg helpers. Bodies are identical to the originals except p_user_id is
--    replaced by auth.uid(). `_document_has_acl_grant_for_user` calls the single-arg
--    `_chunk_acl_grants_user`, so create that one first.
create or replace function public._chunk_acl_grants_user(p_chunk_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.chunk_acl ca
    where ca.chunk_id = p_chunk_id
      and (
        (ca.principal_type = 'user' and ca.principal_id = auth.uid())
        or (
          ca.principal_type = 'group'
          and ca.principal_id in (
            select pm.principal_id
            from public.principal_membership pm
            where pm.member_user_id = auth.uid()
          )
        )
      )
  );
$$;

create or replace function public._document_has_acl_grant_for_user(p_document_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.chunks c
    where c.document_id = p_document_id
      and public._chunk_acl_grants_user(c.id)
  );
$$;

create or replace function public._chunk_belongs_to_doc_owner(p_chunk_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.chunks c
    join public.documents d on d.id = c.document_id
    where c.id = p_chunk_id
      and d.user_id = auth.uid()
  );
$$;

-- 2. Repoint every policy to the single-arg helpers. The surrounding predicate
--    (the workspace-membership conjunct on chunks/documents) is preserved verbatim;
--    only the helper arity changes.
alter policy chunks_select_via_acl on public.chunks
  using (
    public._chunk_acl_grants_user(id)
    and public._user_in_document_workspace(document_id)
  );

alter policy documents_select_via_acl on public.documents
  using (
    public._document_has_acl_grant_for_user(id)
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  );

alter policy chunk_acl_select_for_doc_owner on public.chunk_acl
  using (public._chunk_belongs_to_doc_owner(chunk_id));

alter policy chunk_acl_insert_by_doc_owner on public.chunk_acl
  with check (public._chunk_belongs_to_doc_owner(chunk_id));

alter policy chunk_acl_delete_by_doc_owner on public.chunk_acl
  using (public._chunk_belongs_to_doc_owner(chunk_id));

-- 3. Drop the two-arg helpers now that nothing references them. Drop the dependent
--    (`_document_has_acl_grant_for_user`, whose body called the two-arg
--    `_chunk_acl_grants_user`) before its dependency.
drop function if exists public._document_has_acl_grant_for_user(uuid, uuid);
drop function if exists public._chunk_acl_grants_user(uuid, uuid);
drop function if exists public._chunk_belongs_to_doc_owner(uuid, uuid);
