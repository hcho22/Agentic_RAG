import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { AppHeader } from '@/components/AppHeader'
import { ConversationDetail } from '@/components/support/ConversationDetail'
import { useToast } from '@/components/ui/toast'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'
import {
  listEscalatedConversations,
  resolveActiveWorkspace,
  resolveClaimerEmails,
  subscribeToConversations,
  type ActiveWorkspace,
  type ClaimFields,
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
  const { user } = useAuth()
  const currentUserId = user?.id ?? null

  const [workspace, setWorkspace] = useState<ActiveWorkspace | null>(null)
  const [conversations, setConversations] = useState<ConversationRow[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // uid -> email for the "Claimed by <email>" label (US-089). Resolved lazily
  // from `profiles` as claimed rows appear; a self-claim never needs a lookup.
  const [claimerEmails, setClaimerEmails] = useState<Record<string, string>>({})
  // Claimer uids already handed to a `profiles` lookup — including ones with no
  // `profiles` row, so an unresolvable claimer is requested at most once and the
  // lazy effect can never spin re-fetching it (it stays the generic fallback).
  const requestedClaimerIds = useRef<Set<string>>(new Set())

  const workspaceId =
    workspace?.status === 'resolved' ? workspace.workspaceId : null

  const selected = useMemo(
    () => conversations.find((c) => c.id === selectedId) ?? null,
    [conversations, selectedId],
  )

  // Human-readable claimer label: the current agent is "you"; another agent is
  // shown by email (resolved from `profiles`), falling back to a generic label
  // until (or if) the lookup resolves. Null for an unclaimed conversation.
  const claimerLabel = useCallback(
    (claimedBy: string | null): string | null => {
      if (!claimedBy) return null
      if (claimedBy === currentUserId) return 'you'
      return claimerEmails[claimedBy] ?? 'another agent'
    },
    [currentUserId, claimerEmails],
  )

  // Patch a single conversation in place (optimistic claim/release update). The
  // Realtime feed delivers the same change to every other open queue; keying by
  // id makes the two idempotent.
  const patchConversation = useCallback((id: string, patch: ClaimFields) => {
    setConversations((prev) =>
      prev.map((c) => (c.id === id ? { ...c, ...patch } : c)),
    )
  }, [])

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
        // If the open conversation just left the queue (resolved elsewhere or by
        // this agent), close the detail pane.
        setSelectedId((cur) => (cur === id ? null : cur))
      },
    })
    return unsubscribe
  }, [workspaceId])

  // Resolve emails for any claimer that isn't the current agent and hasn't
  // already been requested. Best-effort: a failed or empty lookup leaves the
  // label as the generic fallback. Each id is marked requested BEFORE the fetch
  // and never re-requested, and setClaimerEmails runs only when the lookup
  // actually returns rows — so an unresolvable claimer never creates a new
  // `claimerEmails` reference that would refire this effect in a loop.
  useEffect(() => {
    const missing = Array.from(
      new Set(
        conversations
          .map((c) => c.claimed_by)
          .filter(
            (id): id is string =>
              !!id &&
              id !== currentUserId &&
              !(id in claimerEmails) &&
              !requestedClaimerIds.current.has(id),
          ),
      ),
    )
    if (missing.length === 0) return
    for (const id of missing) requestedClaimerIds.current.add(id)
    resolveClaimerEmails(missing)
      .then((map) => {
        if (Object.keys(map).length > 0) {
          setClaimerEmails((prev) => ({ ...prev, ...map }))
        }
      })
      .catch(() => {
        // Non-fatal — the row still shows a generic "another agent" claimer.
      })
  }, [conversations, currentUserId, claimerEmails])

  // Full-width status note for the empty/loading/none/ambiguous cases; null when
  // the two-pane list+detail should render instead.
  const statusNote = useMemo<ReactNode | null>(() => {
    if (!workspace) return <StatusNote>Loading…</StatusNote>
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
    if (loading) return <StatusNote>Loading queue…</StatusNote>
    if (conversations.length === 0) {
      return (
        <StatusNote>
          No escalated conversations. Handoffs appear here live the moment a
          conversation is escalated.
        </StatusNote>
      )
    }
    return null
  }, [workspace, loading, conversations])

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <AppHeader />
      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col overflow-hidden px-6 py-8">
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
        {statusNote ? (
          statusNote
        ) : (
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-6 lg:grid-cols-[minmax(260px,340px)_1fr]">
            <ul className="min-h-0 space-y-2 overflow-y-auto pr-1">
              {conversations.map((c) => (
                <QueueRow
                  key={c.id}
                  conversation={c}
                  selected={c.id === selectedId}
                  claimedByMe={!!c.claimed_by && c.claimed_by === currentUserId}
                  claimerLabel={claimerLabel(c.claimed_by)}
                  onSelect={() => setSelectedId(c.id)}
                />
              ))}
            </ul>
            <div className="min-h-0">
              {selected ? (
                <ConversationDetail
                  key={selected.id}
                  conversation={selected}
                  currentUserId={currentUserId}
                  claimerLabel={claimerLabel(selected.claimed_by)}
                  onClaimChange={patchConversation}
                  onResolved={(id) =>
                    setSelectedId((cur) => (cur === id ? null : cur))
                  }
                />
              ) : (
                <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-neutral-800 bg-neutral-900/30 px-4 py-10 text-center text-sm text-neutral-500">
                  Select a conversation to read its transcript and reply.
                </div>
              )}
            </div>
          </div>
        )}
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

function QueueRow({
  conversation,
  selected,
  claimedByMe,
  claimerLabel,
  onSelect,
}: {
  conversation: ConversationRow
  selected: boolean
  claimedByMe: boolean
  claimerLabel: string | null
  onSelect: () => void
}) {
  const { id, escalated_at, customer_email, channel, claimed_by, created_at } = conversation
  // Dim a row another agent has claimed (advisory "someone's likely on this") —
  // but never the one you're reading (selected) or your own claim. It never
  // gates selection or reply; it is a visual de-emphasis only (US-089).
  const claimedByOther = !!claimed_by && !claimedByMe
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={cn(
          'w-full rounded-lg border px-4 py-3 text-left transition-colors',
          selected
            ? 'border-neutral-500 bg-neutral-800/80'
            : 'border-neutral-800 bg-neutral-900/60 hover:bg-neutral-800/50',
          claimedByOther && !selected && 'opacity-60',
        )}
      >
        <div className="flex items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-2">
            <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">
              Escalated
            </span>
            <span className="font-mono text-sm text-neutral-300">{id.slice(0, 8)}</span>
            {channel ? (
              <span className="truncate text-xs text-neutral-500">via {channel}</span>
            ) : null}
          </div>
          <span className="shrink-0 text-xs text-neutral-500">
            {escalated_at
              ? `escalated ${relativeTime(escalated_at)}`
              : `opened ${relativeTime(created_at)}`}
          </span>
        </div>
        <div className="mt-2 flex items-center gap-3 text-xs text-neutral-400">
          {customer_email ? (
            <span className="truncate">
              <span className="text-neutral-500">Customer:</span> {customer_email}
            </span>
          ) : (
            <span className="text-neutral-500">No email left</span>
          )}
          {claimed_by ? (
            <span
              className={cn(
                'shrink-0 truncate',
                claimedByMe ? 'text-emerald-300/90' : 'text-neutral-500',
              )}
            >
              • Claimed by {claimerLabel}
            </span>
          ) : null}
        </div>
      </button>
    </li>
  )
}
