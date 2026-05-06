-- US-020: keyword search infrastructure for hybrid retrieval.
--
-- chunks.content_tsv is a STORED generated column derived from
-- to_tsvector('english', content). Generated columns are auto-maintained on
-- every insert/update without a trigger, which keeps the ingestion path
-- (US-008/US-015) untouched — the existing chunk inserts in main.py just
-- start populating this column for free.
--
-- The 'english' configuration is hard-coded for now: the corpus is mostly
-- English (per PRD intent) and switching configurations later requires a
-- column rebuild anyway. If we add multilingual ingestion, revisit this with
-- a `regconfig` argument and a separate column per language or pg_trgm.
--
-- A GIN index on the tsvector serves the @@ predicate in keyword_search and
-- the upcoming hybrid_search (US-021). GIN is the standard choice for
-- tsvector — fast lookups, slower writes, but ingestion is far less hot than
-- query.

-- The explicit `::regconfig` cast pins the IMMUTABLE overload of
-- to_tsvector. Without it, Postgres can resolve to the (text,text) variant,
-- which is STABLE and rejected for STORED generated columns.
alter table public.chunks
  add column content_tsv tsvector
  generated always as (to_tsvector('english'::regconfig, content)) stored;

create index chunks_content_tsv_gin_idx
  on public.chunks
  using gin (content_tsv);
