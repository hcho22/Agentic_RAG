-- US-021: extend keyword_search with the same US-017 metadata filters as
-- match_chunks so hybrid_search can apply filters consistently to both
-- halves. Without this, a filtered hybrid query would bias toward keyword
-- matches across all docs (filtered vector pool + unfiltered keyword pool).
--
-- Filter semantics intentionally mirror match_chunks
-- (20260421120100_match_chunks_filters.sql): topics use the JSONB ?| "has any
-- key" operator (GIN-indexed), document_type is a scalar equality, date
-- bounds are inclusive and exclude rows with null published_date.
--
-- Drop the 2-arg signature so PostgREST always resolves to the new one —
-- same pattern US-017 used to evolve match_chunks.

create or replace function public.keyword_search(
  query text,
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
set search_path = public, pg_temp
as $$
  with q as (
    select websearch_to_tsquery('english'::regconfig, coalesce(query, '')) as tsq
  )
  select
    c.id,
    c.document_id,
    c.chunk_index,
    c.content,
    ts_rank_cd(c.content_tsv, q.tsq)::float as similarity,
    d.filename
  from public.chunks c
  join public.documents d on d.id = c.document_id
  cross join q
  where c.content_tsv @@ q.tsq
    and d.deleted_at is null
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
  order by ts_rank_cd(c.content_tsv, q.tsq) desc, c.id asc
  limit greatest(match_count, 0);
$$;

drop function if exists public.keyword_search(text, int);

grant execute on function public.keyword_search(
  text, int, text[], text, date, date
) to authenticated;
