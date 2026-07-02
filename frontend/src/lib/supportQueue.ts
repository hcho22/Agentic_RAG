import { supabase } from '@/lib/supabase'

// The agent-reply endpoint (US-082) is the ONE authenticated `/widget/*` route;
// the reply is posted to the backend under the agent's real Supabase JWT (the
// membership RLS is enforced there, under that JWT). Everything else the queue
// touches — the escalated list, the transcript, the Resolve status flip — is a
// direct Supabase read/write under the same JWT.
const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(
  /\/$/,
  '',
)

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

// US-088: the conversation-view surface — read the transcript, reply, Resolve.

export type ConversationMessageRole = 'user' | 'assistant' | 'system' | 'tool'

export type ConversationMessage = {
  id: string
  role: ConversationMessageRole
  content: string | null
  created_at: string
}

// tool_calls / tool_call_id / name are intentionally NOT selected: they are
// null/unused for widget conversations (the deflection pipeline is deterministic
// control flow, not the agentic tool loop, US-066) and the operator transcript
// never renders the tool-call tree (US-088 AC4).
const MESSAGE_COLUMNS = 'id, role, content, created_at'

// Reads the FULL transcript under the agent's own JWT. The
// `conversation_messages_select_member` RLS delegates to the parent
// conversation's workspace membership (presence only, `role` in no predicate),
// so a non-member of the workspace reads zero rows — the same tenant boundary
// the queue list rides. Ordered oldest-first, as the customer sees it (US-071).
export async function listConversationMessages(
  conversationId: string,
): Promise<ConversationMessage[]> {
  const { data, error } = await supabase
    .from('conversation_messages')
    .select(MESSAGE_COLUMNS)
    .eq('conversation_id', conversationId)
    .order('created_at', { ascending: true })
  if (error) throw error
  return (data ?? []) as ConversationMessage[]
}

// Posts an agent reply through the US-082 backend endpoint (the ONE
// authenticated `/widget/*` route). The backend writes the row UNDER THE AGENT'S
// JWT (so the membership RLS is the authorization) then fans it to the
// customer's live SSE (US-081). We forward the caller's Supabase access token so
// that JWT reaches the backend; a cross-workspace agent is rejected there (404).
export async function sendAgentReply(
  conversationId: string,
  content: string,
): Promise<ConversationMessage> {
  const { data: sess } = await supabase.auth.getSession()
  const token = sess.session?.access_token
  if (!token) throw new Error('Not signed in.')

  const res = await fetch(
    `${BACKEND_URL}/widget/conversations/${conversationId}/agent-reply`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ content }),
    },
  )
  if (!res.ok) {
    let detail = `Reply failed (${res.status})`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body?.detail) detail = body.detail
    } catch {
      // non-JSON error body — keep the status-code fallback message.
    }
    throw new Error(detail)
  }
  const body = (await res.json()) as { message: ConversationMessage }
  return body.message
}

// US-089: optional, UNENFORCED soft-claim. Claiming stamps `claimed_by` /
// `claimed_at` on the conversation so the queue can dim a row another agent is
// likely working, reducing accidental double-replies. It is advisory ONLY —
// last-write-wins, and it does NOT gate the reply/resolve actions (anyone in the
// workspace may still reply). This is NOT a routing/assignment axis (S5 deferred,
// ADR-0004/0008). The write rides the SAME `conversations_update_member`
// membership RLS as Resolve (US-088) — a non-member's write affects zero rows —
// and the claim propagates to every other agent's queue live via the existing
// `conversations` Realtime feed (the claim is a plain UPDATE, status unchanged).

export type ClaimFields = {
  claimed_by: string | null
  claimed_at: string | null
}

// Stamp the claim as the current agent. `claimed_by` is set to the caller's own
// auth uid and `claimed_at` to the client clock — the DB does NOT enforce
// `claimed_by = auth.uid()` (the claim is deliberately advisory), so this is the
// honest value, not a trust boundary. Returns the persisted fields for immediate
// optimistic UI (Realtime delivers the same change to every other open queue).
export async function claimConversation(conversationId: string): Promise<ClaimFields> {
  const { data: sess } = await supabase.auth.getSession()
  const userId = sess.session?.user?.id
  if (!userId) throw new Error('Not signed in.')

  const { data, error } = await supabase
    .from('conversations')
    .update({ claimed_by: userId, claimed_at: new Date().toISOString() })
    .eq('id', conversationId)
    .select('claimed_by, claimed_at')
    .single()
  if (error) throw error
  return data as ClaimFields
}

// Release a claim (clear `claimed_by` / `claimed_at`). Unenforced like the claim
// itself; the UI only offers this to the current claimer, but the DB imposes no
// such restriction (membership is the only boundary).
export async function releaseConversation(conversationId: string): Promise<ClaimFields> {
  const { data, error } = await supabase
    .from('conversations')
    .update({ claimed_by: null, claimed_at: null })
    .eq('id', conversationId)
    .select('claimed_by, claimed_at')
    .single()
  if (error) throw error
  return data as ClaimFields
}

// Batch-resolve claimer uids to emails for the "Claimed by <email>" label. Reads
// `public.profiles` (the auth.users(id,email) mirror, US-037; RLS select-true) so
// a claimer's identity can be shown without touching the auth schema. Best-effort
// at the call site — an unresolved id falls back to a generic "another agent".
export async function resolveClaimerEmails(
  ids: string[],
): Promise<Record<string, string>> {
  if (ids.length === 0) return {}
  const { data, error } = await supabase
    .from('profiles')
    .select('id, email')
    .in('id', ids)
  if (error) throw error
  const out: Record<string, string> = {}
  for (const row of (data ?? []) as { id: string; email: string }[]) {
    out[row.id] = row.email
  }
  return out
}

// Resolve = the one-way latch into the terminal `resolved` status (US-067). The
// UPDATE runs under the agent's JWT (`conversations_update_member` RLS) and
// touches ONLY `status` — the US-067 BEFORE trigger enforces escalated→resolved
// is legal, and the US-071 AFTER trigger purges the customer's opaque reconnect
// token so the widget's stored token is invalidated (its live SSE closes on the
// next revalidation and a resume is rejected). We never write `conversation_tokens`
// from the client (it is deny-all RLS); the purge is a pure DB-side consequence.
export async function resolveConversation(conversationId: string): Promise<void> {
  const { error } = await supabase
    .from('conversations')
    .update({ status: 'resolved' })
    .eq('id', conversationId)
  if (error) throw error
}
