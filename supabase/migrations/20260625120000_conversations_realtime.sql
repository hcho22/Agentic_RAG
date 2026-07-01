-- US-087: enable Supabase Realtime for public.conversations so the operator
-- queue (/support/queue) can live-update as conversations flip
-- active → escalated → resolved, under the agent's OWN JWT — no polling, no
-- backend fan-out, and (per ADR-0008) NO customer-leg Realtime (the anonymous
-- customer stays off the Supabase Realtime surface; only workspace MEMBERS
-- subscribe here).
--
-- Realtime honours RLS for INSERT and UPDATE events: every subscriber only
-- receives those change events for rows they can SELECT under
-- conversations_select_member (US-066 — the ADR-0002 workspace-membership clause,
-- `role` in no predicate). So the client-side `workspace_id=eq.<id>` filter is a
-- convenience that cuts wire chatter, NOT the security boundary — a non-member of
-- the workspace receives ZERO INSERT/UPDATE events even if they craft a channel
-- for it (the US-087 validation test: U2 ∉ W sees none of W's conversations).
--
-- Caveat — DELETE is NOT RLS-filtered by Realtime: a DELETE event's `payload.old`
-- (the full row, since `replica identity full` below) is delivered to ANY
-- subscriber whose `workspace_id` filter matches, regardless of membership. This
-- is currently UNREACHABLE and therefore not an active leak: no code path deletes
-- a conversation (the US-067 status machine makes `resolved` terminal and the app
-- only UPDATEs status), so no DELETE event is ever emitted. Reassess this before
-- any future story adds a conversation-delete path. (The accepted
-- 20260417170000 documents-realtime precedent carries the same DELETE caveat.)
--
-- `conversation_messages` is deliberately NOT published here; the customer
-- transcript/reply fan-out is the backend-SSE path (US-081/082), and the queue
-- only needs conversation-row status transitions.

alter publication supabase_realtime add table public.conversations;

-- REPLICA IDENTITY FULL means UPDATE events carry the full pre-image in
-- `payload.old` (so the client can tell an active→escalated transition from an
-- escalated→resolved one without refetching) and DELETE events carry the row id.
-- The table is small and writes are conversation-driven, so the extra WAL cost
-- is negligible — the same trade-off documents made in 20260417170000.
alter table public.conversations replica identity full;
