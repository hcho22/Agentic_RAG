import { supabase } from '@/lib/supabase'

// US-087: the operator support queue reads and live-subscribes to
// `public.conversations` DIRECTLY under the agent's own Supabase JWT — the same
// authenticated Realtime pattern the Ingestion list uses for `documents`
// (see lib/ingestion.ts). The US-066 membership RLS (conversations_select_member)
// is the trust boundary on BOTH the initial read and the Realtime feed, so a
// non-member of the workspace retrieves and receives zero rows. `role` gates
// nothing here — the queue is membership-gated, not role-gated (ADR-0002/0004).

export type ConversationStatus = 'active' | 'escalated' | 'resolved'
export type WorkspaceRole = 'admin' | 'member'

export type ConversationRow = {
  id: string
  workspace_id: string
  status: ConversationStatus
  channel: string
  customer_email: string | null
  // Set-once by the US-067 status trigger on the first transition into
  // `escalated`; null while `active`. Drives the queue ordering.
  escalated_at: string | null
  // US-089 (optional soft-claim) populates these; surfaced here as read-only.
  claimed_by: string | null
  claimed_at: string | null
  created_at: string
}

const CONVERSATION_COLUMNS =
  'id, workspace_id, status, channel, customer_email, escalated_at, claimed_by, claimed_at, created_at'

// US-007 active-workspace resolution, applied CLIENT-SIDE for the queue:
// default-when-sole, error-on-ambiguous, explicit "none". Reads the caller's
// own `workspace_membership` rows (RLS: workspace_membership_select_own scopes
// the result to auth.uid()), so this never crosses the tenant boundary. There
// is no cross-workspace inbox in v1 — a single active workspace per view — so an
// ambiguous (≥2) membership set is surfaced as a resolvable UI state rather than
// silently guessing a workspace (mirrors the backend's 400-on-ambiguous posture).
export type ActiveWorkspace =
  | { status: 'resolved'; workspaceId: string; role: WorkspaceRole }
  | { status: 'none' }
  | { status: 'ambiguous'; workspaceIds: string[] }

export async function resolveActiveWorkspace(): Promise<ActiveWorkspace> {
  const { data, error } = await supabase
    .from('workspace_membership')
    .select('workspace_id, role')
    .order('created_at', { ascending: true })
  if (error) throw error

  const rows = (data ?? []) as { workspace_id: string; role: WorkspaceRole }[]
  if (rows.length === 0) return { status: 'none' }
  if (rows.length === 1) {
    return { status: 'resolved', workspaceId: rows[0].workspace_id, role: rows[0].role }
  }
  return { status: 'ambiguous', workspaceIds: rows.map((r) => r.workspace_id) }
}

// Lists the workspace's escalated conversations, oldest-escalation-first so the
// longest-waiting customer sits at the top of the handoff queue (FIFO). RLS
// backstops the workspace_id filter — a non-member gets 0 rows regardless.
export async function listEscalatedConversations(
  workspaceId: string,
): Promise<ConversationRow[]> {
  const { data, error } = await supabase
    .from('conversations')
    .select(CONVERSATION_COLUMNS)
    .eq('workspace_id', workspaceId)
    .eq('status', 'escalated')
    .order('escalated_at', { ascending: true })
  if (error) throw error
  return (data ?? []) as ConversationRow[]
}

export type ConversationRealtimeHandlers = {
  // A conversation entered (or was born in) the escalated state — add/refresh it.
  onEscalated?: (row: ConversationRow) => void
  // A conversation left the escalated state (resolved) or its row was deleted —
  // drop it from the queue by id.
  onLeft?: (id: string) => void
}

// Live-subscribe to the workspace's conversation-row changes via the agent's own
// Supabase Realtime `postgres_changes` (RLS-honoured — a non-member receives
// nothing). The client filters on `workspace_id` to cut wire chatter; the
// escalated/left branching happens in the handler off each event's new status.
// Returns an unsubscribe function suitable as a React effect cleanup.
export function subscribeToConversations(
  workspaceId: string,
  handlers: ConversationRealtimeHandlers,
): () => void {
  const channel = supabase
    .channel(`conversations:${workspaceId}`)
    .on(
      'postgres_changes',
      {
        event: '*',
        schema: 'public',
        table: 'conversations',
        filter: `workspace_id=eq.${workspaceId}`,
      },
      (payload) => {
        if (payload.eventType === 'DELETE') {
          const id = (payload.old as { id?: string } | null)?.id
          if (id) handlers.onLeft?.(id)
          return
        }
        const row = payload.new as ConversationRow
        if (row.status === 'escalated') {
          handlers.onEscalated?.(row)
        } else {
          // active (not yet in the queue) or resolved (leaving it).
          handlers.onLeft?.(row.id)
        }
      },
    )
    .subscribe()

  return () => {
    void supabase.removeChannel(channel)
  }
}
