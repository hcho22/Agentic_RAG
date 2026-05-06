-- US-020: keyword_search RPC — Postgres full-text counterpart to match_chunks.
--
-- Returns the top-`match_count` chunks whose content_tsv matches the parsed
-- query, ranked by ts_rank_cd. Shape mirrors match_chunks so US-021's
-- hybrid_search can fuse the two result sets without per-side projection.
--
-- websearch_to_tsquery is preferred over plainto_tsquery: it accepts the
-- conventions users already type ("quoted phrases", OR, -negation) and never
-- raises on malformed input — it just yields a query that matches nothing,
-- which is the right failure mode for an agent-supplied string.
--
-- Soft-deleted documents are excluded (matches match_chunks). SECURITY
-- INVOKER + the existing chunks RLS policies keep cross-user rows invisible
-- when called via PostgREST RPC under the user's JWT.
--
-- The `similarity` column carries the ts_rank_cd score so the return shape is
-- identical to match_chunks. The two scores are NOT directly comparable
-- (cosine in [0,1] vs. unbounded rank score), but US-021 RRF fuses by rank
-- position, not score magnitude, so this is fine for downstream callers.

create or replace function public.keyword_search(
  query text,
  match_count int default 5
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
  order by ts_rank_cd(c.content_tsv, q.tsq) desc, c.id asc
  limit greatest(match_count, 0);
$$;

grant execute on function public.keyword_search(text, int) to authenticated;
