import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchTranscript,
  streamWidgetMessage,
  subscribeConversationEvents,
  type TranscriptMessage,
} from './api'
import {
  clearConversation,
  getConversationId,
  getConversationToken,
  setConversationId,
  setConversationToken,
} from './storage'

// US-084: the chat conversation state machine for the in-iframe widget.
//
// Drives one anonymous support conversation: sending a turn streams the US-079
// `delta` events into an optimistic assistant bubble, the first reply mints + stores
// the US-071 token, and the durable transcript (US-071) is the source of truth that
// reconciles the optimistic rows and recovers agent replies.
//
// LIVE agent-reply transport: US-081's customer SSE
// (`GET /widget/conversations/{id}/events`, authorized by the opaque token) pushes
// each agent reply as a low-latency nudge that feeds `refreshTranscript` - the same
// seam the interim poll feeds. The poll is RETAINED as a backstop (a dropped/closed
// SSE never loses a reply: the durable US-071 transcript recovers it), so the SSE
// only makes replies feel instant. The customer never holds a Supabase Realtime
// channel - the stream is a plain backend SSE off the Supabase trust surface.

const DEFAULT_API_BASE = (
  import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

// Poll cadence (ms) for agent-reply recovery; a backstop behind US-081's SSE push.
const POLL_INTERVAL_MS = 8000

export interface WidgetMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  error?: boolean
}

export interface ConversationState {
  messages: WidgetMessage[]
  sending: boolean
  error: string | null
  /** Epoch ms until which the composer is throttled (US-076 429), or null. */
  throttledUntil: number | null
  /** Conversation status: active | escalated | resolved (US-067). */
  status: string
  send: (text: string) => Promise<void>
}

function toWidgetMessages(rows: TranscriptMessage[]): WidgetMessage[] {
  return rows.map((m) => ({ id: m.id, role: m.role, content: m.content ?? '' }))
}

export function useConversation(publicKey: string): ConversationState {
  // The backend base is ALWAYS the kit's build-time value, never host-supplied -
  // the token is sent here, so the host (or an XSS on it) must not control it.
  const apiBase = DEFAULT_API_BASE

  const [messages, setMessages] = useState<WidgetMessage[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [throttledUntil, setThrottledUntil] = useState<number | null>(null)
  const [status, setStatus] = useState<string>('active')

  const conversationIdRef = useRef<string | null>(null)
  const sendingRef = useRef(false)
  sendingRef.current = sending

  // Bumped at the start of every send. A transcript fetch captures the generation
  // before its `await` and discards its result if a send has begun since - so a
  // stale snapshot can never clobber the in-flight optimistic/streaming rows. The
  // deliberate post-send reconcile shares its send's generation, so it still applies.
  const sendGenerationRef = useRef(0)

  const refreshTranscript = useCallback(async () => {
    const id = conversationIdRef.current
    const token = getConversationToken(publicKey)
    if (!id || !token) return
    const generation = sendGenerationRef.current
    const res = await fetchTranscript({ apiBase, conversationId: id, token })
    if (sendGenerationRef.current !== generation) {
      // A send started while this fetch was in flight, so this snapshot predates its
      // optimistic/streaming rows (and any token it just minted). Discard it rather
      // than clobbering them; the send owns the message list and handles its own
      // token expiry, and its post-send reconcile (same generation) still applies.
      return
    }
    if (res.status === 401) {
      // Token dead (expired/resolved): clear it and STOP polling (nulling the id
      // trips the poll guard). The composer stays usable - the next send starts a
      // fresh conversation rather than dead-ending the customer.
      clearConversation(publicKey)
      conversationIdRef.current = null
      return
    }
    if (res.status === 200 && res.conversation && res.messages) {
      setStatus(res.conversation.status)
      setMessages(toWidgetMessages(res.messages))
    }
    // Other statuses (0 network / 5xx) are transient - keep polling.
  }, [apiBase, publicKey])

  // Resume on mount: if a token + id are stored from a prior session, recover the
  // transcript (the iframe-origin storage survives reloads).
  useEffect(() => {
    const token = getConversationToken(publicKey)
    const id = getConversationId(publicKey)
    if (token && id) {
      conversationIdRef.current = id
      void refreshTranscript()
    }
  }, [publicKey, refreshTranscript])

  // Agent-reply poll - now a BACKSTOP behind US-081's SSE (below). Still runs so a
  // dropped/closed SSE never loses a reply; pauses while hidden or mid-stream.
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.hidden) return
      if (sendingRef.current) return
      if (!conversationIdRef.current) return
      void refreshTranscript()
    }, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [refreshTranscript])

  // US-081 live push: hold the customer SSE open whenever a conversation + token
  // exist, reconnecting on drop. A `message` nudge re-reads the durable transcript
  // (the SSE carries no authoritative content - the transcript is the source of
  // truth); a `close` (resolved) refreshes once to capture the final state. The poll
  // above remains the backstop, so this loop is purely a latency win.
  useEffect(() => {
    let cancelled = false
    let controller: AbortController | null = null
    const wait = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms))

    async function loop() {
      while (!cancelled) {
        const id = conversationIdRef.current
        const token = getConversationToken(publicKey)
        if (!id || !token) {
          // No conversation yet (first send not sent, or token cleared). Re-check
          // cheaply - no network until there is something to subscribe to.
          await wait(1000)
          continue
        }
        controller = new AbortController()
        const { status } = await subscribeConversationEvents({
          apiBase,
          conversationId: id,
          token,
          signal: controller.signal,
          onEvent: (evt) => {
            if (evt.kind === 'message' || evt.kind === 'close') {
              if (sendingRef.current) return
              void refreshTranscript()
            }
          },
        })
        if (cancelled) break
        // A 401 means the token is dead (expired/resolved). Clear it ourselves so a
        // backgrounded tab - whose interim poll is paused by `document.hidden` and so
        // never clears it - stops re-opening the SSE on a dead conversation; the
        // `!id || !token` guard then idles until the next send starts a fresh one.
        // Skip while a send is in flight: it owns token expiry and may have just
        // minted a fresh token, exactly as the poll's refreshTranscript guards. Any
        // other end is a normal drop/close; back off longer on 401 to avoid hot-looping.
        if (status === 401 && !sendingRef.current) {
          clearConversation(publicKey)
          conversationIdRef.current = null
        }
        await wait(status === 401 ? 3000 : 1500)
      }
    }
    void loop()
    return () => {
      cancelled = true
      controller?.abort()
    }
  }, [publicKey, apiBase, refreshTranscript])

  // Auto-clear the throttle latch once its window elapses, so the composer
  // re-enables and WidgetApp's countdown interval (keyed on throttledUntil) tears
  // down instead of ticking for the widget's lifetime after the first 429.
  useEffect(() => {
    if (throttledUntil === null) return
    const remaining = throttledUntil - Date.now()
    if (remaining <= 0) {
      setThrottledUntil(null)
      return
    }
    const timer = setTimeout(() => setThrottledUntil(null), remaining)
    return () => clearTimeout(timer)
  }, [throttledUntil])

  // Stream one attempt into the placeholder bubble. Returns the outcome so the
  // caller can decide whether to retry (a dead token) or surface an error.
  const runTurn = useCallback(
    async (
      message: string,
      botId: string,
      token: string | null,
    ): Promise<'ok' | 'expired' | 'error'> => {
      let streamed = ''
      try {
        for await (const evt of streamWidgetMessage({ apiBase, publicKey, token, message })) {
          if (evt.kind === 'token') {
            setConversationToken(publicKey, evt.token)
          } else if (evt.kind === 'conversation') {
            conversationIdRef.current = evt.conversation.id
            setConversationId(publicKey, evt.conversation.id)
            setStatus(evt.conversation.status)
          } else if (evt.kind === 'delta') {
            streamed += evt.text
            setMessages((prev) =>
              prev.map((m) => (m.id === botId ? { ...m, content: streamed } : m)),
            )
          } else if (evt.kind === 'done') {
            setMessages((prev) =>
              prev.map((m) => (m.id === botId ? { ...m, streaming: false } : m)),
            )
          } else if (evt.kind === 'error') {
            if (evt.status === 401) {
              // The stored token is dead (expired/resolved). Clear it; the caller
              // retries this same message ONCE as a fresh first message.
              clearConversation(publicKey)
              conversationIdRef.current = null
              return 'expired'
            }
            if (evt.status === 429 && evt.retryAfterSeconds) {
              setThrottledUntil(Date.now() + evt.retryAfterSeconds * 1000)
            }
            setError(evt.message)
            return 'error'
          }
        }
        return 'ok'
      } catch {
        setError('Sorry, something went wrong. Please try again.')
        return 'error'
      }
    },
    [apiBase, publicKey],
  )

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || sendingRef.current) return
      if (throttledUntil && Date.now() < throttledUntil) return

      sendGenerationRef.current += 1
      setError(null)
      const localUserId = `local-u-${Date.now()}`
      const localBotId = `local-a-${Date.now()}`
      setMessages((prev) => [
        ...prev,
        { id: localUserId, role: 'user', content: trimmed },
        { id: localBotId, role: 'assistant', content: '', streaming: true },
      ])
      setSending(true)

      try {
        let outcome = await runTurn(trimmed, localBotId, getConversationToken(publicKey))
        // A dead token is the common 24h-expiry case: transparently restart the
        // SAME message as a brand-new conversation rather than dead-ending the user.
        if (outcome === 'expired') {
          setError(null)
          outcome = await runTurn(trimmed, localBotId, null)
        }
        if (outcome === 'ok') {
          // Reconcile against the authoritative transcript (real ids) so the poll
          // diffs cleanly and optimistic rows become persisted ones.
          await refreshTranscript()
        } else {
          // Drop an empty placeholder; flag a partially-streamed one as errored.
          setMessages((prev) =>
            prev
              .filter((m) => !(m.id === localBotId && !m.content))
              .map((m) => (m.id === localBotId ? { ...m, streaming: false, error: true } : m)),
          )
        }
      } finally {
        setSending(false)
      }
    },
    [publicKey, throttledUntil, runTurn, refreshTranscript],
  )

  return { messages, sending, error, throttledUntil, status, send }
}
