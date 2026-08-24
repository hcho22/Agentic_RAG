-- SEC-F2 (audit data/purvia-rls-audit/report.md §3): constrain
-- documents.workspace_id on WRITE so a document can only be placed into — or kept
-- in — a workspace the writer belongs to.
--
-- BEFORE: documents_insert_own / documents_update_own (20260417120000) gate ONLY
-- ownership (`auth.uid() = user_id`); nothing constrains the workspace_id column.
-- So an authenticated user could INSERT a document they own carrying an ARBITRARY
-- (FK-valid) workspace_id — including a workspace they are NOT a member of, e.g.
-- the well-known Default Workspace — or UPDATE their own document's workspace_id
-- to move it across the tenant boundary. Today the SUBTRACTIVE read boundary
-- (the workspace-membership conjunct on the SELECT policies and inside
-- match_chunks/keyword_search) keeps that mis-tagged row invisible to everyone,
-- so it is not a content leak in isolation — but it is a write-integrity hole:
-- the whole tenancy design treats documents.workspace_id as trustworthy, and any
-- future feature that reads it under the service role (analytics / billing-by-
-- workspace) or any read policy that ever widens to "workspace members regardless
-- of owner" would turn it into a real cross-tenant write/inject.
--
-- FIX: AND the SAME workspace-membership EXISTS clause the READ side already uses
-- (20260708120000) onto the write predicates, so the write boundary matches the
-- read boundary — a writer may only create/keep a document in a workspace they
-- are a member of. This is a TIGHTENING (RLS stays on; the predicates only gain a
-- conjunct). Owner reassignment stays impossible: the existing
-- `auth.uid() = user_id` check in both USING and WITH CHECK still pins user_id to
-- the caller. The membership test reads documents.workspace_id off the row itself
-- (not a self-referential re-scan), mirroring the 20260708120000 read policy so
-- an INSERT ... RETURNING evaluates it against the row's own column cleanly.
--
-- Over the Default Workspace (every legacy user + doc is a member,
-- 20260617120200) the new conjunct is a no-op, so existing legitimate uploads are
-- unaffected; it only bites a write that targets a workspace the caller does not
-- belong to.

alter policy documents_insert_own on public.documents
  with check (
    auth.uid() = user_id
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  );

alter policy documents_update_own on public.documents
  using (
    auth.uid() = user_id
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  )
  with check (
    auth.uid() = user_id
    and exists (
      select 1
      from public.workspace_membership wm
      where wm.workspace_id = documents.workspace_id
        and wm.user_id = auth.uid()
    )
  );
