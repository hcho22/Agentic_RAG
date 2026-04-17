-- US-010: match_chunks RPC backing the search_documents tool.
--
-- Returns the top-k chunks above `match_threshold` ranked by cosine similarity
-- against a query embedding. Joins documents to surface filename and skips
-- soft-deleted rows.
--
-- SECURITY INVOKER (default) keeps RLS active, so cross-user chunks stay
-- invisible even when this function is called via PostgREST RPC. The HNSW
-- index from 20260417140000_add_chunks_embedding.sql handles the <=> ordering
-- sub-linearly once the corpus grows.
--
-- Embedding dimension is pinned to 1536 (text-embedding-3-small). Switching
-- to text-embedding-3-large (3072) requires updating both the chunks.embedding
-- column and this function signature, then reapplying both migrations.

create or replace function public.match_chunks(
  query_embedding vector(1536),
  match_threshold float default 0.3,
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
  order by c.embedding <=> query_embedding asc
  limit greatest(match_count, 0);
$$;

grant execute on function public.match_chunks(vector(1536), float, int)
  to authenticated;
