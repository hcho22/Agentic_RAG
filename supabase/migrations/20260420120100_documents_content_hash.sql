-- US-014: content-addressable hashing on documents for per-user dedup.
-- chunks.content_hash is added separately in 20260420120000_chunks_content_hash.sql
-- (US-015's incremental-reingest diff).
--
-- The unique index is partial on two fronts:
--   * deleted_at is null — a soft-deleted upload must not block a user from
--     re-adding the same file later.
--   * content_hash is not null — rows created before this migration have no
--     hash yet; leaving them out of the index avoids a spurious collision on
--     the first row to be backfilled.

alter table public.documents
  add column content_hash text;

create unique index documents_user_id_content_hash_idx
  on public.documents (user_id, content_hash)
  where deleted_at is null and content_hash is not null;
