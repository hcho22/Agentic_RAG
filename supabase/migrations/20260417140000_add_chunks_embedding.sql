-- US-009: vector embeddings on chunks.
--
-- Default column dimension is 1536 to match text-embedding-3-small
-- (backend EMBEDDING_MODEL default). To switch to text-embedding-3-large,
-- replace vector(1536) with vector(3072) here and re-apply — pgvector
-- column dimensions are fixed at DDL time.
--
-- HNSW is preferred over IVFFlat (PRD Technical Considerations) because it
-- gives sub-linear kNN without a training step. vector_cosine_ops pairs
-- with the <=> operator we use at query time (US-010+).

alter table public.chunks
  add column embedding vector(1536);

create index chunks_embedding_hnsw_idx
  on public.chunks
  using hnsw (embedding vector_cosine_ops);
