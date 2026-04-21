-- US-016 / US-017: documents.metadata JSONB for LLM-extracted structured
-- metadata. Schema (enforced in the app layer via Pydantic, not in SQL) is:
--   { title: str | null,
--     authors: str[],
--     topics: str[],
--     published_date: date | null (ISO-8601 YYYY-MM-DD),
--     document_type: str | null }
--
-- Nullable by design — US-016 allows extraction to fail without blocking
-- ingestion. US-017 filters are expressed as JSONB predicates (see the
-- match_chunks signature added in 20260421120100_match_chunks_filters.sql).
--
-- GIN index on the whole blob so equality/containment filters on `topics`,
-- `document_type`, etc. are index-backed. Date-range filters cast
-- metadata->>'published_date' to date and are expected to be selective
-- enough post-topic/type-filter that a functional index isn't needed yet.

alter table public.documents
  add column metadata jsonb;

create index documents_metadata_gin_idx
  on public.documents using gin (metadata);
