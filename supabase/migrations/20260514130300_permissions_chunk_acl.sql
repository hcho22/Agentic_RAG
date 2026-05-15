-- US-037: chunk_acl is the denormalized source of truth for permission
-- grants on individual chunks. Doc-level grants are materialized into one
-- row per chunk at grant time (US-038) so the retrieval predicate stays a
-- single JOIN-less EXISTS subquery rather than walking a doc → chunk path.
--
-- ACLs are additive to ownership. Owners are recognised via chunks.user_id
-- in match_chunks; chunk_acl rows only grant access to additional principals.
-- This keeps the existing single-user corpus working without a row backfill.
--
-- The (principal_id, chunk_id) index is the load-bearing one: it serves the
-- EXISTS (select 1 from chunk_acl where principal_id = ? and chunk_id = c.id)
-- subquery in match_chunks. The composite PK already supplies a (chunk_id,
-- principal_type, principal_id) index for cascade-delete on chunks and for
-- list_doc_shares queries that aggregate by chunk.
--
-- The companion RLS policies on chunks and documents mirror the function
-- predicate so that defense-in-depth holds: even if a future caller invokes
-- match_chunks with a different predicate (or queries the tables directly),
-- ACL'd reads still resolve correctly under SECURITY INVOKER.

create table public.chunk_acl (
  chunk_id uuid not null references public.chunks(id) on delete cascade,
  principal_type text not null check (principal_type in ('user', 'group')),
  principal_id uuid not null,
  granted_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  primary key (chunk_id, principal_type, principal_id)
);

create index chunk_acl_principal_id_chunk_id_idx
  on public.chunk_acl (principal_id, chunk_id);

alter table public.chunk_acl enable row level security;

create policy chunk_acl_select_visible on public.chunk_acl
  for select using (
    (principal_type = 'user' and principal_id = auth.uid())
    or (
      principal_type = 'group'
      and principal_id in (
        select pm.principal_id
        from public.principal_membership pm
        where pm.member_user_id = auth.uid()
      )
    )
  );

-- Defense-in-depth: extend chunks/documents select RLS so a viewer who holds
-- an ACL grant can read the underlying rows. Without these, match_chunks
-- (SECURITY INVOKER) would have its owner-OR-ACL predicate filter out rows
-- that the chunks/documents RLS had already hidden, and ACL'd reads would
-- silently return zero. Both policies OR onto the existing owner policies.
--
-- The predicate is wrapped in a SECURITY DEFINER helper because US-038 adds
-- a doc-owner policy on chunk_acl that queries chunks — without bypassing
-- RLS on the inner read, the policies form a cycle (chunks RLS reads
-- chunk_acl, chunk_acl RLS reads chunks → infinite recursion). The helper
-- runs with the function owner's privileges and skips RLS on the inner read,
-- but still applies the predicate's logic, so the security property is
-- preserved.

create or replace function public._chunk_acl_grants_user(
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
    from public.chunk_acl ca
    where ca.chunk_id = p_chunk_id
      and (
        (ca.principal_type = 'user' and ca.principal_id = p_user_id)
        or (
          ca.principal_type = 'group'
          and ca.principal_id in (
            select pm.principal_id
            from public.principal_membership pm
            where pm.member_user_id = p_user_id
          )
        )
      )
  );
$$;

create or replace function public._document_has_acl_grant_for_user(
  p_document_id uuid,
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
    where c.document_id = p_document_id
      and public._chunk_acl_grants_user(c.id, p_user_id)
  );
$$;

create policy chunks_select_via_acl on public.chunks
  for select using (public._chunk_acl_grants_user(id, auth.uid()));

create policy documents_select_via_acl on public.documents
  for select using (public._document_has_acl_grant_for_user(id, auth.uid()));
