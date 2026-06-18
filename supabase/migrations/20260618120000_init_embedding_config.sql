-- US-026 (ADR-0006): a single-row stamp recording the embedding model + vector
-- dimension the corpus was indexed under. The retrieval index only works if the
-- query embedding and the stored chunk embeddings come from the SAME model:
-- vectors from two models are written in different "languages", so mixing them
-- silently degrades recall with no error (the dangerous same-dims-different-model
-- case — e.g. text-embedding-3-small and text-embedding-ada-002 are both 1536).
-- The DB already knows the dimension (it is baked into chunks.embedding's
-- vector(1536) column) but NOT the model name; this stamp records both so a
-- later embedder change is *detectable* (US-027's startup probe-embed compares
-- the configured embedder against this stamp and refuses to start on a mismatch)
-- instead of rotting retrieval quietly.
--
-- Single-row invariant (one model per corpus, NOT per chunk): the `singleton`
-- boolean is the primary key and is pinned to TRUE by the CHECK, so a second row
-- is impossible — every writer upserts the one row via `on conflict (singleton)`.
--
-- `dim` source of truth: the chunks.embedding column type (vector(1536),
-- supabase/migrations/20260417140000_add_chunks_embedding.sql) remains the
-- source of truth for the dimension — pgvector rejects any insert whose length
-- differs, so by the time a writer stamps `dim` it has necessarily matched the
-- column. The stamp simply records that column dim ALONGSIDE the model name (the
-- one thing the column cannot store).
--
-- Re-embedding vs re-chunking (documented here and in CONTEXT.md): a change that
-- triggers this stamp's detection is a re-*embed* — recomputing the vectors for
-- the SAME chunks under a new model/provider (optionally at a migrated dim).
-- Re-embedding PRESERVES chunk UUIDs, and therefore the chunk_acl grants keyed on
-- those UUIDs survive. This is the safe operation. It is the opposite of
-- re-*chunking* (different chunk size / content edit), which destroys the chunk
-- UUIDs and with them every chunk_acl grant (the re-chunking caveat in
-- docs/permissions-aware-rag.md / CONTEXT.md). The US-027 re-index remedy is
-- therefore a re-embed, not a re-chunk: grants stay intact, plus a column
-- migration only when the dimension itself changes.

create table public.embedding_config (
  singleton boolean primary key default true,
  model text not null,
  dim int not null check (dim > 0),
  indexed_at timestamptz not null default now(),
  constraint embedding_config_singleton check (singleton)
);

alter table public.embedding_config enable row level security;

-- Authenticated callers may READ the stamp (the model name is harmless and the
-- US-027 guard reads it via service-role anyway) but may NOT write it: there is
-- no INSERT/UPDATE/DELETE policy for the API role, so the single row is
-- write-immutable under RLS. ALL writes — both the production ingest stamp and
-- the bulk re-index seeders — run as service-role, which bypasses RLS.
--
-- Why no authenticated INSERT: a `with check (true)` insert policy let ANY
-- authenticated tenant seed the GLOBAL singleton with an arbitrary (model, dim)
-- on an empty corpus. Because the row is then immutable to the API role, a
-- legitimate later ingest no-ops on the poisoned stamp and the US-027 startup
-- guard trips on the mismatch — a cross-tenant startup DoS driven from one
-- tenant. Restricting writes to service-role closes that hole; the production
-- stamp path (_stamp_embedding_config) now writes with the service-role key and
-- skips when it is unset (matching the guard, which also disables itself then).
-- anon gets no policy → no access.
create policy embedding_config_select on public.embedding_config
  for select to authenticated using (true);
