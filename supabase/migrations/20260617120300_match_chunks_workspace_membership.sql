-- US-003 (ADR-0002): enforce the workspace tenant boundary INSIDE match_chunks.
--
-- The boundary is part of the retrieval predicate now, not a backend filter: a
-- chunk is visible iff
--   (owner OR ACL grant) AND viewer ∈ members(document.workspace_id)
-- resolved from auth.uid() against workspace_membership. Because the function is
-- SECURITY INVOKER and reads auth.uid() (never a backend-passed workspace id), a
-- forgotten backend filter can only widen visibility WITHIN the viewer's own
-- workspaces — it can never leak across the boundary.
--
-- The membership clause is AND-ed under the existing owner-OR-ACL predicate, so
-- the boundary is SUBTRACTIVE: even the OWNER of a document loses visibility if
-- they are not a member of that document's workspace. The clause references only
-- d.workspace_id (from the existing documents join, 20260514150000 line 92),
-- auth.uid(), and workspace_membership — no role column (administrative-only,
-- ADR-0002) and no function parameter carrying a workspace id (membership is the
-- boundary, not a passed tenant id — the ADR-0002 rejected alternative).
--
-- This is a DROP-and-CREATE: the body changes but the signature and return shape
-- are byte-identical to 20260514150000_match_chunks_granting_principal.sql, so
-- the GRANT is re-issued unchanged. Over the Default Workspace (every legacy doc
-- + every legacy user backfilled in 20260617120200) the new clause is a no-op,
-- so existing retrieval returns identical rows (E4 passes bit-for-bit); the
-- boundary only bites once a SECOND workspace exists (proven additively by E6).

drop function if exists public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int
);

create function public.match_chunks(
  query_embedding extensions.vector(1536),
  match_threshold float default 0.3,
  match_count int default 5,
  filter_topics text[] default null,
  filter_document_type text default null,
  filter_date_from date default null,
  filter_date_to date default null,
  ef_search int default null
)
returns table (
  id uuid,
  document_id uuid,
  chunk_index int,
  content text,
  similarity float,
  filename text,
  granting_principal_id uuid,
  granting_principal_display text
)
language plpgsql
stable
security invoker
set search_path = public, extensions, pg_temp
as $$
begin
  if ef_search is not null then
    perform set_config('hnsw.ef_search', ef_search::text, true);
  end if;

  return query
  select
    sub.id,
    sub.document_id,
    sub.chunk_index,
    sub.content,
    sub.similarity,
    sub.filename,
    sub.granting_principal_id,
    sub.granting_principal_display
  from (
    select distinct on (c.id)
      c.id,
      c.document_id,
      c.chunk_index,
      c.content,
      (1 - (c.embedding <=> query_embedding))::float as similarity,
      (c.embedding <=> query_embedding) as distance,
      d.filename,
      case
        when c.user_id = auth.uid() then null::uuid
        else ca.principal_id
      end as granting_principal_id,
      case
        when c.user_id = auth.uid() then 'owner'::text
        when ca.principal_type = 'user' then (
          select p.email from public.profiles p where p.id = auth.uid()
        )
        when ca.principal_type = 'group' then (
          select pr.name from public.principals pr where pr.id = ca.principal_id
        )
      end as granting_principal_display
    from public.chunks c
    join public.documents d on d.id = c.document_id
    left join public.chunk_acl ca on ca.chunk_id = c.id
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
    where c.embedding is not null
      and d.deleted_at is null
      and (1 - (c.embedding <=> query_embedding)) >= match_threshold
      and (c.user_id = auth.uid() or ca.chunk_id is not null)
      and exists (
        select 1
        from public.workspace_membership wm
        where wm.workspace_id = d.workspace_id
          and wm.user_id = auth.uid()
      )
      and (
        filter_topics is null
        or (d.metadata ? 'topics' and d.metadata->'topics' ?| filter_topics)
      )
      and (
        filter_document_type is null
        or d.metadata->>'document_type' = filter_document_type
      )
      and (
        filter_date_from is null
        or (d.metadata->>'published_date')::date >= filter_date_from
      )
      and (
        filter_date_to is null
        or (d.metadata->>'published_date')::date <= filter_date_to
      )
    order by
      c.id,
      case
        when c.user_id = auth.uid() then 1
        when ca.principal_type = 'user' then 2
        when ca.principal_type = 'group' then 3
      end asc,
      ca.created_at asc nulls first,
      ca.principal_id asc nulls first
  ) sub
  order by sub.distance asc
  limit greatest(match_count, 0);
end;
$$;

grant execute on function public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int
) to authenticated;
