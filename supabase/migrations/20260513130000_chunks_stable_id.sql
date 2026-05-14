-- US-032: stable_id on public.chunks so the retrieval-eval golden set (US-033)
-- can key on an identifier that survives re-seeds and clean CI runs.
--
-- Purely additive: the column is nullable so existing chunks (uploaded by
-- real users via the UI) keep working without a backfill. Only rows inserted
-- by db_seed/corpus_seed.py populate stable_id. The unique index treats
-- multiple NULLs as distinct (Postgres default), so the partial-population
-- pattern is fine.
--
-- Shape: "{filename_slug}:{chunk_index}", e.g. "refund-policy:0",
-- "shipping-faq:3". Human-readable so eval failures debug cleanly.

alter table public.chunks
  add column stable_id text;

create unique index chunks_stable_id_idx
  on public.chunks (stable_id)
  where stable_id is not null;
