-- US-037: replace match_chunks with the owner-OR-ACL predicate plus an
-- optional ef_search knob.
--
-- Predicate change: rows are visible to the caller if they own the chunk
-- (chunks.user_id = auth.uid()) OR if a chunk_acl row grants the caller
-- access either directly (principal_type='user', principal_id=auth.uid())
-- or via group membership (principal_type='group', principal_id in the
-- caller's principal_membership set). The check is restated in the function
-- body for explicitness even though the chunks/documents RLS already mirrors
-- it — this keeps the function self-documenting and tolerant to future RLS
-- relaxations elsewhere.
--
-- ef_search: optional. When set, perform set_config('hnsw.ef_search', ...,
-- true) before the SELECT so the change is local to the transaction. This
-- is the knob US-042's scale benchmark uses to chart selective-filter recall
-- against HNSW search effort. Null leaves the session/server default in
-- place and the function behaves identically to the pre-permission version
-- on the existing single-user corpus.
--
-- Switching language from `sql` to `plpgsql` so the conditional set_config
-- call can be expressed as a `perform` statement before the query.
--
-- Return shape unchanged in this story; the granting-principal column ships
-- in US-041.

create or replace function public.match_chunks(
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
  filename text
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
    c.id,
    c.document_id,
    c.chunk_index,
    c.content,
    (1 - (c.embedding <=> query_embedding))::float as similarity,
    d.filename
  from public.chunks c
  join public.documents d on d.id = c.document_id
  where c.embedding is not null
    and d.deleted_at is null
    and (1 - (c.embedding <=> query_embedding)) >= match_threshold
    and (
      c.user_id = auth.uid()
      or exists (
        select 1
        from public.chunk_acl ca
        where ca.chunk_id = c.id
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
      )
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
  order by c.embedding <=> query_embedding asc
  limit greatest(match_count, 0);
end;
$$;

-- Drop the previous 7-arg signature so PostgREST always resolves to the new
-- 8-arg form (an overload would make the RPC ambiguous).
drop function if exists public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date
);

grant execute on function public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int
) to authenticated;
