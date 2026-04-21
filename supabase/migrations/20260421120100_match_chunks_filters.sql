-- US-017: extend match_chunks RPC with optional metadata filters.
--
-- New parameters (all default null → no filter applied):
--   filter_topics        text[]  — match if documents.metadata->'topics' contains ANY of these
--   filter_document_type text    — equality on documents.metadata->>'document_type'
--   filter_date_from     date    — metadata->>'published_date' >= filter_date_from
--   filter_date_to       date    — metadata->>'published_date' <= filter_date_to
--
-- Topics use the `?|` JSONB "has any key" operator so the GIN index from
-- 20260421120000_documents_metadata.sql can serve the predicate. Document-
-- type is a scalar equality. Date filters cast the stored ISO-8601 string
-- to date; rows with null/missing published_date are excluded whenever a
-- date filter is supplied (explicit-intent semantics: "only dated docs").
--
-- SECURITY INVOKER + unchanged return shape preserve RLS and the
-- SearchDocumentsResult contract from US-010.

create or replace function public.match_chunks(
  query_embedding extensions.vector(1536),
  match_threshold float default 0.3,
  match_count int default 5,
  filter_topics text[] default null,
  filter_document_type text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  id uuid,
  document_id uuid,
  chunk_index int,
  content text,
  similarity float,
  filename text
)
language sql
stable
security invoker
set search_path = public, extensions, pg_temp
as $$
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
$$;

-- Drop the old 3-arg signature so PostgREST always resolves to the new one.
drop function if exists public.match_chunks(extensions.vector(1536), float, int);

grant execute on function public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date
) to authenticated;
