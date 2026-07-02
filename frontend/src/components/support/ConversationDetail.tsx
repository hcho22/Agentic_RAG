import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Dialog } from '@/components/ui/dialog'
import { useToast } from '@/components/ui/toast'
import {
  claimConversation,
  listConversationMessages,
  releaseConversation,
  resolveConversation,
  sendAgentReply,
  type ClaimFields,
  type ConversationMessage,
  type ConversationRow,
} from '@/lib/supportQueue'

// US-088: the operator-side conversation view. The agent reads the full
// transcript (under their own JWT + membership RLS), replies through the US-082
// endpoint (which fans the reply to the customer's live SSE), and Resolves to
// close the handoff (terminal `status='resolved'`, which purges the customer's
// reconnect token, US-067/071).
//
// conversation_messages is deliberately NOT on the Supabase Realtime publication
// (US-087 — only `conversations` is), so the agent's transcript is kept current
// with a light poll while the view is open rather than a Realtime channel; the
// customer leg gets the low-latency push (US-081), the operator gets a poll.
const TRANSCRIPT_POLL_MS = 5000

// Only the customer/support turns are rendered — the same customer-visible slice
// the transcript endpoint constrains to (US-071, role in user|assistant). system
// and tool rows (and the tool_calls tree) are never shown (US-088 AC4).
function isVisible(message: ConversationMessage): boolean {
  return message.role === 'user' || message.role === 'assistant'
}

function formatTime(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  return new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function ConversationDetail({
  conversation,
  currentUserId,
  claimerLabel,
  onClaimChange,
  onResolved,
}: {
  conversation: ConversationRow
  currentUserId: string | null
  // Human-readable claimer identity resolved by the queue: 'you' for a self
  // claim, an email for another agent, or null when unclaimed.
  claimerLabel: string | null
  onClaimChange: (id: string, patch: ClaimFields) => void
  onResolved: (id: string) => void
}) {
  const { toast } = useToast()
  const conversationId = conversation.id
  const isResolved = conversation.status === 'resolved'

  const claimedBy = conversation.claimed_by
  const claimedByMe = !!claimedBy && claimedBy === currentUserId

  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [confirmResolve, setConfirmResolve] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [claiming, setClaiming] = useState(false)

  const scrollRef = useRef<HTMLDivElement>(null)

  const loadMessages = useCallback(
    async (opts?: { quiet?: boolean }) => {
      if (!opts?.quiet) setLoading(true)
      try {
        const rows = await listConversationMessages(conversationId)
        setMessages(rows.filter(isVisible))
      } catch (e) {
        // A quiet poll failure is transient — don't spam a toast on every tick.
        if (!opts?.quiet) {
          toast(e instanceof Error ? e.message : 'Failed to load the transcript', 'error')
        }
      } finally {
        if (!opts?.quiet) setLoading(false)
      }
    },
    [conversationId, toast],
  )

  // Load on open + reset composer when the selected conversation changes.
  useEffect(() => {
    setDraft('')
    setConfirmResolve(false)
    void loadMessages()
  }, [conversationId, loadMessages])

  // Light poll to surface customer follow-ups while the view is open (no
  // Realtime on conversation_messages by design). Stops once resolved.
  useEffect(() => {
    if (isResolved) return
    const id = window.setInterval(() => {
      void loadMessages({ quiet: true })
    }, TRANSCRIPT_POLL_MS)
    return () => window.clearInterval(id)
  }, [isResolved, loadMessages])

  // Keep the transcript pinned to the newest message.
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const send = useCallback(async () => {
    const content = draft.trim()
    if (!content || sending) return
    setSending(true)
    try {
      const reply = await sendAgentReply(conversationId, content)
      setDraft('')
      // Merge the durable reply immediately (dedupe by id in case a poll raced).
      setMessages((prev) =>
        prev.some((m) => m.id === reply.id) ? prev : [...prev, reply],
      )
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to send the reply', 'error')
    } finally {
      setSending(false)
    }
  }, [draft, sending, conversationId, toast])

  const onComposerKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter sends; Shift+Enter inserts a newline.
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        void send()
      }
    },
    [send],
  )

  const doResolve = useCallback(async () => {
    setResolving(true)
    try {
      await resolveConversation(conversationId)
      setConfirmResolve(false)
      toast('Conversation resolved.', 'default')
      onResolved(conversationId)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to resolve', 'error')
    } finally {
      setResolving(false)
    }
  }, [conversationId, onResolved, toast])

  // Claim / release is the UNENFORCED soft-claim (US-089): it only stamps
  // claimed_by/claimed_at so other agents see the row dimmed. It NEVER gates the
  // reply or resolve controls below — those stay enabled regardless of who (if
  // anyone) holds the claim. Claiming an already-claimed conversation just takes
  // it over (last-write-wins).
  const toggleClaim = useCallback(async () => {
    if (claiming) return
    setClaiming(true)
    try {
      const patch = claimedByMe
        ? await releaseConversation(conversationId)
        : await claimConversation(conversationId)
      onClaimChange(conversationId, patch)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to update the claim', 'error')
    } finally {
      setClaiming(false)
    }
  }, [claiming, claimedByMe, conversationId, onClaimChange, toast])

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-neutral-800 bg-neutral-900/60">
      <header className="flex items-center justify-between gap-4 border-b border-neutral-800 px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm text-neutral-200">
              {conversationId.slice(0, 8)}
            </span>
            {isResolved ? (
              <span className="rounded-full bg-neutral-700/60 px-2 py-0.5 text-xs text-neutral-300">
                Resolved
              </span>
            ) : (
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">
                Escalated
              </span>
            )}
            {claimedBy ? (
              <span
                className={
                  claimedByMe
                    ? 'rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300'
                    : 'rounded-full bg-neutral-700/50 px-2 py-0.5 text-xs text-neutral-300'
                }
              >
                Claimed by {claimerLabel}
              </span>
            ) : null}
          </div>
          <p className="mt-0.5 truncate text-xs text-neutral-500">
            {conversation.customer_email
              ? conversation.customer_email
              : 'No email left'}
            {conversation.channel ? ` • via ${conversation.channel}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* Advisory soft-claim toggle — never gates reply/resolve (US-089). */}
          {!isResolved ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void toggleClaim()}
              disabled={claiming}
            >
              {claiming
                ? claimedByMe
                  ? 'Releasing…'
                  : 'Claiming…'
                : claimedByMe
                  ? 'Release'
                  : claimedBy
                    ? 'Claim anyway'
                    : 'Claim'}
            </Button>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setConfirmResolve(true)}
            disabled={isResolved || resolving}
          >
            {isResolved ? 'Resolved' : 'Resolve'}
          </Button>
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 min-h-0 space-y-3 overflow-y-auto px-4 py-4">
        {loading ? (
          <p className="py-8 text-center text-sm text-neutral-500">Loading transcript…</p>
        ) : messages.length === 0 ? (
          <p className="py-8 text-center text-sm text-neutral-500">
            No messages yet in this conversation.
          </p>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} />)
        )}
      </div>

      <div className="border-t border-neutral-800 px-4 py-3">
        {isResolved ? (
          <p className="text-center text-xs text-neutral-500">
            This conversation is resolved. The customer’s session has been closed.
          </p>
        ) : (
          <div className="flex items-end gap-2">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onComposerKeyDown}
              rows={2}
              placeholder="Reply to the customer…"
              disabled={sending}
            />
            <Button
              onClick={() => void send()}
              disabled={sending || draft.trim().length === 0}
              className="shrink-0"
            >
              {sending ? 'Sending…' : 'Send'}
            </Button>
          </div>
        )}
      </div>

      <Dialog
        open={confirmResolve}
        onOpenChange={(o) => {
          if (!resolving) setConfirmResolve(o)
        }}
        title="Resolve this conversation?"
        description="Resolving is final. It closes the customer’s session and invalidates their widget so they can’t resume this conversation — they’d have to start a new one."
      >
        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setConfirmResolve(false)}
            disabled={resolving}
          >
            Cancel
          </Button>
          <Button size="sm" onClick={() => void doResolve()} disabled={resolving}>
            {resolving ? 'Resolving…' : 'Resolve'}
          </Button>
        </div>
      </Dialog>
    </section>
  )
}

function MessageBubble({ message }: { message: ConversationMessage }) {
  // Support (assistant) turns — both the bot's deflection answers and human
  // agent replies share role='assistant' (US-082: no separate 'agent' role, no
  // author column), so they render identically as "Support".
  const isSupport = message.role === 'assistant'
  return (
    <div className={isSupport ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={
          isSupport
            ? 'max-w-[80%] rounded-lg bg-emerald-600/20 px-3 py-2 text-sm text-emerald-50'
            : 'max-w-[80%] rounded-lg bg-neutral-800 px-3 py-2 text-sm text-neutral-100'
        }
      >
        <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-neutral-400">
          <span>{isSupport ? 'Support' : 'Customer'}</span>
          <span className="text-neutral-500">{formatTime(message.created_at)}</span>
        </div>
        <p className="whitespace-pre-wrap break-words">{message.content ?? ''}</p>
      </div>
    </div>
  )
}
