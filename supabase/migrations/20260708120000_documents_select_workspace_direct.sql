-- US-005 follow-up: fix the documents upload read-back 403 (PostgREST 42501)
-- introduced by the workspace-membership conjunct added in 20260617120500.
--
-- Root cause - INSERT...RETURNING command visibility:
-- supabase-js inserts with return=representation (.insert(row).select().single()),
-- so Postgres applies the documents SELECT policy to the freshly-inserted
-- RETURNING row. Both SELECT policies gated in-workspace membership through the
-- SECURITY DEFINER helper _user_in_document_workspace(id), which RE-READS
-- public.documents by id to discover the row's workspace_id. During the same
-- INSERT statement that row is not yet command-visible to that self-referential
-- sub-scan, so the EXISTS returns false and the read-back is denied 42501/403 -
-- regardless of which workspace_id the row carries. The upload therefore appears
-- to fail for every legitimate member of the row's own workspace.
--
-- Fix - read the workspace_id off the row itself:
-- a documents row carries its OWN workspace_id column, which IS present in the
-- RETURNING row (and in any ordinary SELECT). So the in-workspace conjunct can
-- test membership against documents.workspace_id directly instead of re-scanning
-- documents by id. This preserves the exact same effective boundary
-- ((owner OR acl) AND in-workspace) - only the mechanism of the in-workspace
-- conjunct changes - and removes the self-referential read that the INSERT could
-- not satisfy. Cross-workspace zero-leak is unchanged: a non-member still fails
-- the EXISTS against workspace_membership and reads zero rows.
--
-- The chunks policies (chunks_select_own / chunks_select_via_acl) are deliberately
-- left on _user_in_document_workspace(document_id): chunks have no own
-- workspace_id and must resolve it via the parent documents row, and chunks are
-- inserted by the backend service role (RLS bypassed), so they never hit this
-- INSERT...RETURNING visibility problem. The helper stays in place for them.

alter policy documents_select_own on public.documents
  using (
    auth.uid() = user_id
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  );

alter policy documents_select_via_acl on public.documents
  using (
    public._document_has_acl_grant_for_user(id, auth.uid())
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  );
