-- US-015: chunks.content_hash enables incremental re-ingestion — the backend
-- diffs new chunks against existing ones by SHA-256 of the chunk text so only
-- new/modified chunks get re-embedded.
--
-- Nullable on purpose: rows ingested before this migration have no hash and
-- get re-embedded on their next re-ingest (which populates the hash going
-- forward). US-014 will later add documents.content_hash + the per-user
-- unique index for document-level dedup; kept separate so that migration can
-- land independently.
--
-- Index is (document_id, content_hash) because the diff query is always
-- scoped to a single document.

alter table public.chunks
  add column content_hash text;

create index chunks_document_hash_idx
  on public.chunks (document_id, content_hash);
