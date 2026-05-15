-- US-038: extend chunk_acl RLS so the document owner can list, grant, and
-- revoke shares on their own document via the user JWT (no service-role
-- escalation). The base US-037 policies only grant *the principal* the right
-- to see their own grants; they don't let the doc owner see who they've
-- shared with, or write new grants under their JWT.
--
-- All three policies share the same predicate: "this chunk belongs to a
-- document owned by auth.uid()". They OR onto the existing select policy.
--
-- These policies are intentionally additive rather than written into the
-- US-037 chunk_acl migration — keeping the data model migration narrowly
-- scoped to the data model and pushing operation-level RLS into the story
-- that introduces the operations preserves a clean revert boundary.
--
-- The predicate is wrapped in a SECURITY DEFINER helper to break a policy
-- cycle: chunks_select_via_acl (US-037) reads chunk_acl, and these policies
-- read chunks — without skipping RLS on the inner read, Postgres detects
-- "infinite recursion in policy" and fails the query. The helper runs with
-- the function owner's privileges, bypasses RLS on the inner read, but
-- still applies the same ownership predicate, so security is preserved.

create or replace function public._chunk_belongs_to_doc_owner(
  p_chunk_id uuid,
  p_user_id uuid
)
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
      and d.user_id = p_user_id
  );
$$;

create policy chunk_acl_select_for_doc_owner on public.chunk_acl
  for select using (public._chunk_belongs_to_doc_owner(chunk_id, auth.uid()));

create policy chunk_acl_insert_by_doc_owner on public.chunk_acl
  for insert with check (public._chunk_belongs_to_doc_owner(chunk_id, auth.uid()));

create policy chunk_acl_delete_by_doc_owner on public.chunk_acl
  for delete using (public._chunk_belongs_to_doc_owner(chunk_id, auth.uid()));
