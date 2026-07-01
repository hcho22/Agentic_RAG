import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { AppHeader } from '@/components/AppHeader'
import { useToast } from '@/components/ui/toast'
import {
  listEscalatedConversations,
  resolveActiveWorkspace,
  subscribeToConversations,
  type ActiveWorkspace,
  type ConversationRow,
} from '@/lib/supportQueue'

// Normalize escalated_at to an epoch so the queue orders by ACTUAL time
// regardless of source encoding: PostgREST returns ISO 8601 ('T' separator)
// while Supabase Realtime's payload.new commonly uses a space separator — a raw
// string compare would sort every Realtime row before every REST row (' ' < 'T')
// and let a freshly-escalated conversation jump the queue. A null/unparseable
// value sorts LAST (Infinity) so it never displaces a real, ordered row.
function escalatedEpoch(row: ConversationRow): number {
  if (!row.escalated_at) return Number.POSITIVE_INFINITY
  const t = new Date(row.escalated_at).getTime()
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t
}

// Oldest-escalation-first: keep the list sorted by escalated_at ascending as
// live events arrive so a freshly-escalated conversation slots into the right
// place instead of always jumping to the top/bottom.
function insertSorted(rows: ConversationRow[], row: ConversationRow): ConversationRow[] {
  const without = rows.filter((r) => r.id !== row.id)
  const next = [...without, row]
  next.sort((a, b) => escalatedEpoch(a) - escalatedEpoch(b))
  return next
}

function relativeTime(iso: string | null): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return `${days}d ago`
}

export function SupportQueuePage() {
  const { toast } = useToast()

  const [workspace, setWorkspace] = useState<ActiveWorkspace | null>(null)
  const [conversations, setConversations] = useState<ConversationRow[]>([])
  const [loading, setLoading] = useState(true)

  const workspaceId =
    workspace?.status === 'resolved' ? workspace.workspaceId : null

  // Resolve the active workspace once (default-when-sole / ambiguous / none).
  useEffect(() => {
    let cancelled = false
    resolveActiveWorkspace()
      .then((w) => {
        if (!cancelled) setWorkspace(w)
      })
      .catch((e) => {
        if (cancelled) return
        toast(e instanceof Error ? e.message : 'Failed to resolve workspace', 'error')
        setWorkspace({ status: 'none' })
      })
    return () => {
      cancelled = true
    }
  }, [toast])

  const refresh = useCallback(
    async (wsId: string) => {
      setLoading(true)
      try {
        const rows = await listEscalatedConversations(wsId)
        setConversations(rows)
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to load the queue', 'error')
      } finally {
        setLoading(false)
      }
    },
    [toast],
  )

  useEffect(() => {
    if (!workspaceId) return
    void refresh(workspaceId)
  }, [workspaceId, refresh])

  // Live-subscribe: an active→escalated transition adds the conversation to the
  // queue in real time; escalated→resolved (or a delete) removes it. RLS on the
  // Realtime feed keeps this scoped to the caller's workspace.
  useEffect(() => {
    if (!workspaceId) return
    const unsubscribe = subscribeToConversations(workspaceId, {
      onEscalated: (row) => {
        setConversations((prev) => insertSorted(prev, row))
      },
      onLeft: (id) => {
        setConversations((prev) => prev.filter((c) => c.id !== id))
      },
    })
    return unsubscribe
  }, [workspaceId])

  const body = useMemo(() => {
    if (!workspace) {
      return <StatusNote>Loading…</StatusNote>
    }
    if (workspace.status === 'none') {
      return (
        <StatusNote>
          You don’t belong to any workspace, so there’s no support queue to show.
        </StatusNote>
      )
    }
    if (workspace.status === 'ambiguous') {
      return (
        <StatusNote>
          You belong to multiple workspaces. The support queue shows a single active
          workspace at a time, and there’s no workspace selector yet — cross-workspace
          inboxes aren’t supported in v1.
        </StatusNote>
      )
    }
    if (loading) {
      return <StatusNote>Loading queue…</StatusNote>
    }
    if (conversations.length === 0) {
      return (
        <StatusNote>
          No escalated conversations. Handoffs appear here live the moment a
          conversation is escalated.
        </StatusNote>
      )
    }
    return (
      <ul className="space-y-3">
        {conversations.map((c) => (
          <QueueRow key={c.id} conversation={c} />
        ))}
      </ul>
    )
  }, [workspace, loading, conversations])

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 overflow-y-auto px-6 py-8">
        <div className="mb-6 flex items-baseline justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold">Support queue</h2>
            <p className="mt-1 text-sm text-neutral-400">
              Conversations that have been escalated to a human, live for every member
              of your workspace.
            </p>
          </div>
          {workspace?.status === 'resolved' && !loading ? (
            <span className="shrink-0 rounded-full bg-neutral-800 px-3 py-1 text-xs text-neutral-300">
              {conversations.length} waiting
            </span>
          ) : null}
        </div>
        {body}
      </main>
    </div>
  )
}

function StatusNote({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-4 py-10 text-center text-sm text-neutral-400">
      {children}
    </div>
  )
}

function QueueRow({ conversation }: { conversation: ConversationRow }) {
  const { id, escalated_at, customer_email, channel, claimed_by, created_at } = conversation
  return (
    <li className="rounded-lg border border-neutral-800 bg-neutral-900/60 px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">
            Escalated
          </span>
          <span className="font-mono text-sm text-neutral-300">{id.slice(0, 8)}</span>
          {channel ? (
            <span className="text-xs text-neutral-500">via {channel}</span>
          ) : null}
        </div>
        <span className="shrink-0 text-xs text-neutral-500">
          {escalated_at ? `escalated ${relativeTime(escalated_at)}` : `opened ${relativeTime(created_at)}`}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-3 text-xs text-neutral-400">
        {customer_email ? (
          <span>
            <span className="text-neutral-500">Customer:</span> {customer_email}
          </span>
        ) : (
          <span className="text-neutral-500">No email left</span>
        )}
        {claimed_by ? (
          <span className="text-neutral-500">• Claimed</span>
        ) : null}
      </div>
    </li>
  )
}
