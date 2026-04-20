-- US-013: enable Supabase Realtime for public.documents so the Ingestion UI
-- can subscribe to live status transitions (queued → processing → ready/error)
-- without polling. Realtime honours RLS — every subscriber only receives
-- rows they can SELECT, so the user_id filter in the client is a convenience
-- (cuts wire chatter), not a security boundary.

alter publication supabase_realtime add table public.documents;

-- REPLICA IDENTITY FULL means UPDATE events carry the full pre-image of the
-- row in `payload.old`, which lets the client detect meaningful transitions
-- (e.g. 'processing' → 'error', or deleted_at flipping null → timestamp)
-- without refetching. The table is small and writes are user-driven, so the
-- extra WAL cost is negligible.
alter table public.documents replica identity full;
