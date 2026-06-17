-- US-005 (ADR-0002): mirror the workspace-membership boundary into the chunks
-- and documents SELECT RLS, so even a DIRECT PostgREST table read (or a future
-- caller invoking a different function) cannot cross the workspace boundary.
-- This is defense-in-depth: US-003/US-004 put the clause inside match_chunks /
-- keyword_search (SECURITY INVOKER), but a client can also read /rest/v1/chunks
-- and /rest/v1/documents directly, bypassing those functions. The same pattern
-- Module 11 used for chunk_acl (20260514130300 / 20260514140000).
--
-- SECURITY DEFINER helper to break the policy-recursion cycle: the chunks /
-- documents SELECT policies need to read documents + workspace_membership, but
-- documents itself carries the policy we are amending — a SECURITY INVOKER read
-- would re-enter that policy and Postgres would raise "infinite recursion in
-- policy". Running the lookup in a DEFINER function (owner = superuser) bypasses
-- RLS on the inner reads while still applying the membership predicate, exactly
-- like _chunk_acl_grants_user / _chunk_belongs_to_doc_owner.
create or replace function public._user_in_document_workspace(
  p_document_id uuid
)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.workspace_membership wm
    join public.documents d on d.id = p_document_id
    where wm.workspace_id = d.workspace_id
      and wm.user_id = auth.uid()
  );
$$;

-- AND the membership check onto EVERY existing permissive SELECT branch. Postgres
-- OR-s permissive policies together, so a NEW permissive policy would *widen*
-- visibility (disjoin) and break the boundary; the boundary must conjoin. We
-- therefore amend each existing policy in place (the owner branch AND the
-- ..._via_acl branch each additionally require workspace membership), giving the
-- effective predicate (owner OR acl) AND in-workspace — matching match_chunks.
--
-- chunks rows carry document_id; documents rows carry their own id. Over the
-- Default Workspace the helper is a no-op (every legacy doc + user is a member).

alter policy chunks_select_own on public.chunks
  using (
    auth.uid() = user_id
    and public._user_in_document_workspace(document_id)
  );

alter policy chunks_select_via_acl on public.chunks
  using (
    public._chunk_acl_grants_user(id, auth.uid())
    and public._user_in_document_workspace(document_id)
  );

alter policy documents_select_own on public.documents
  using (
    auth.uid() = user_id
    and public._user_in_document_workspace(id)
  );

alter policy documents_select_via_acl on public.documents
  using (
    public._document_has_acl_grant_for_user(id, auth.uid())
    and public._user_in_document_workspace(id)
  );
