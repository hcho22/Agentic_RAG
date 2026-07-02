// US-084: the widget's backend client, scoped to the anonymous public surface.
//
// Three endpoints, all authed by the US-071 opaque per-conversation token (NOT a
// Supabase JWT - the anonymous customer is off the Supabase trust surface):
//
//   * POST /widget/conversations/messages - the customer turn. With no token it is
//     the FIRST message (US-078 lazily creates the conversation and returns the raw
//     token ONCE in the `X-Conversation-Token` RESPONSE HEADER); with the token it
//     RESUMES. The response is a request-scoped SSE the bot answer streams over as
//     `conversation` / `delta` / `done` / `error` events (US-079) - the SAME shape
//     /api/chat uses.
//   * GET /widget/conversations/{id}/transcript - the durable message history
//     (US-071), used to recover prior messages on open AND, as a backstop behind
//     US-081's live push, to surface agent replies via a light poll.
//   * GET /widget/conversations/{id}/events - the long-lived customer SSE (US-081)
//     that pushes each async agent reply as a low-latency nudge to re-read the
//     transcript (`subscribeConversationEvents`); the poll above is retained as a
//     backstop so a dropped stream never loses a reply.
//
// The token lives in the iframe's own-origin localStorage (storage.ts) and is sent
// in the `X-Conversation-Token` request header - it never appears in a URL/log.

export interface PublicConversation {
  id: string
  status: string
  created_at: string
}

export interface TranscriptMessage {
  id: string
  role: 'user' | 'assistant'
  content: string | null
  created_at: string
}

export type WidgetStreamEvent =
  // The first-message raw token, lifted from the response header (US-071/078).
  | { kind: 'token'; token: string }
  | { kind: 'conversation'; conversation: PublicConversation }
  | { kind: 'delta'; text: string }
  | { kind: 'done' }
  | { kind: 'error'; message: string; status?: number; retryAfterSeconds?: number }

const CONVERSATION_TOKEN_HEADER = 'X-Conversation-Token'

/**
 * Stream one customer turn. Yields a `token` event first when the response carries
 * a fresh `X-Conversation-Token` (first message only), then the SSE events. The
 * raw token is surfaced only to the caller (which stores it in iframe localStorage)
 * - never rendered, never logged.
 */
export async function* streamWidgetMessage(opts: {
  apiBase: string
  publicKey: string
  token: string | null
  message: string
}): AsyncGenerator<WidgetStreamEvent> {
  const { apiBase, publicKey, token, message } = opts
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  // Resume when we already hold a token; otherwise it is the first message and the
  // public key selects the workspace whose bot answers.
  if (token) headers[CONVERSATION_TOKEN_HEADER] = token
  const body = token
    ? JSON.stringify({ message })
    : JSON.stringify({ message, public_key: publicKey })

  let res: Response
  try {
    res = await fetch(`${apiBase}/widget/conversations/messages`, {
      method: 'POST',
      headers,
      body,
    })
  } catch {
    yield { kind: 'error', message: 'Network error - please try again.' }
    return
  }

  if (!res.ok || !res.body) {
    if (res.status === 429) {
      const retry = Number(res.headers.get('Retry-After')) || 30
      yield {
        kind: 'error',
        message: 'You are sending messages too quickly. Please wait a moment.',
        status: 429,
        retryAfterSeconds: retry,
      }
      return
    }
    if (res.status === 401) {
      // The stored token is invalid/expired/resolved - the cue to start fresh.
      yield { kind: 'error', message: 'This conversation has ended.', status: 401 }
      return
    }
    yield {
      kind: 'error',
      message: 'Sorry, something went wrong. Please try again.',
      status: res.status,
    }
    return
  }

  // First-message token rides the response header (never an SSE event/body, US-071).
  const fresh = res.headers.get(CONVERSATION_TOKEN_HEADER)
  if (fresh) yield { kind: 'token', token: fresh }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const evt = parseSSE(raw)
        if (!evt) continue
        if (evt.event === 'conversation') {
          yield { kind: 'conversation', conversation: evt.data as unknown as PublicConversation }
        } else if (evt.event === 'delta' && typeof evt.data.text === 'string') {
          yield { kind: 'delta', text: evt.data.text }
        } else if (evt.event === 'done') {
          yield { kind: 'done' }
        } else if (evt.event === 'error') {
          yield { kind: 'error', message: String(evt.data.message ?? 'Unknown error') }
        }
      }
    }
  } finally {
    try {
      await reader.cancel()
    } catch {
      /* already closed/errored - nothing to release */
    }
  }
}

export interface EscalateResult {
  /** HTTP status, or 0 on a network error. A 401 means the token is dead; a 400
   *  means the OPTIONAL email was malformed (US-092). */
  status: number
  conversation?: PublicConversation
}

/**
 * US-091: the explicit "talk to a human" escalation. Calls the US-080 latch
 * endpoint (`POST /widget/conversations/escalate`), authed by the opaque token -
 * the deterministic, UI-INITIATED counterpart to the model-mediated escalate
 * decision, NEVER a model `escalate()` tool. On success the conversation is latched
 * `status='escalated'` and the bot goes silent thereafter; every later customer
 * message routes to the human queue. A missing/expired/resolved token → 401 (the
 * cue to start fresh); a latch-write failure → 502.
 *
 * US-092: `email` is an OPTIONAL follow-up address ("leave your email and a human
 * will follow up") sent as `customer_email` metadata for MANUAL follow-up (v1 sends
 * no automated email). It NEVER gates the handoff — a blank/omitted email still
 * escalates; a malformed one comes back 400 so the caller can prompt a fix. The
 * escalate is idempotent, so calling it purely to attach an email to an
 * already-escalated (e.g. model-mediated) conversation is safe.
 */
export async function escalateConversation(opts: {
  apiBase: string
  token: string
  email?: string | null
}): Promise<EscalateResult> {
  const { apiBase, token, email } = opts
  let res: Response
  try {
    res = await fetch(`${apiBase}/widget/conversations/escalate`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        [CONVERSATION_TOKEN_HEADER]: token,
      },
      body: JSON.stringify({ customer_email: email ?? null }),
    })
  } catch {
    return { status: 0 }
  }
  if (!res.ok) return { status: res.status }
  try {
    const body = (await res.json()) as { conversation: PublicConversation }
    return { status: 200, conversation: body.conversation }
  } catch {
    // A 2xx with an unreadable body still means the latch succeeded; the caller
    // marks the conversation escalated regardless.
    return { status: 200 }
  }
}

export interface TranscriptResult {
  /** HTTP status, or 0 on a network error. A 401 means the token is dead (the
   *  poll should stop and the conversation be cleared). */
  status: number
  conversation?: PublicConversation
  messages?: TranscriptMessage[]
}

/** Fetch the durable transcript for a conversation (US-071), authed by the token. */
export async function fetchTranscript(opts: {
  apiBase: string
  conversationId: string
  token: string
}): Promise<TranscriptResult> {
  const { apiBase, conversationId, token } = opts
  let res: Response
  try {
    res = await fetch(`${apiBase}/widget/conversations/${conversationId}/transcript`, {
      headers: { [CONVERSATION_TOKEN_HEADER]: token },
    })
  } catch {
    return { status: 0 }
  }
  if (!res.ok) return { status: res.status }
  try {
    const body = (await res.json()) as {
      conversation: PublicConversation
      messages: TranscriptMessage[]
    }
    return { status: 200, conversation: body.conversation, messages: body.messages }
  } catch {
    return { status: 0 }
  }
}

// US-081: a live agent-reply arriving on the customer SSE. `message` is a nudge
// ("a new row exists, re-read the transcript") rather than the content itself - the
// durable US-071 transcript stays the single source of truth, so the consumer just
// refreshes. `close` means the conversation was resolved (its token purged); `ready`
// confirms the channel is live.
export type ConversationEvent =
  | { kind: 'ready' }
  | { kind: 'message' }
  | { kind: 'close'; reason: string }

/**
 * Open the long-lived customer SSE (US-081) for one conversation, authorized by the
 * US-071 opaque token in the `X-Conversation-Token` header. `EventSource` cannot set
 * headers, so this uses fetch + a ReadableStream (the same SSE-over-fetch parsing as
 * `streamWidgetMessage`) to keep the token in a header, never a URL/log.
 *
 * Resolves when the stream ends - closed by the server (`close`), dropped, aborted
 * via `signal`, or a non-OK status (returned as `status`, so a 401 stops the caller's
 * reconnect loop). Best-effort: a dropped push is recovered by the transcript poll.
 */
export async function subscribeConversationEvents(opts: {
  apiBase: string
  conversationId: string
  token: string
  signal: AbortSignal
  onEvent: (evt: ConversationEvent) => void
}): Promise<{ status: number }> {
  const { apiBase, conversationId, token, signal, onEvent } = opts
  let res: Response
  try {
    res = await fetch(`${apiBase}/widget/conversations/${conversationId}/events`, {
      headers: { [CONVERSATION_TOKEN_HEADER]: token, Accept: 'text/event-stream' },
      signal,
    })
  } catch {
    // Network error or an abort (unmount / reconnect) - report as transient.
    return { status: 0 }
  }
  if (!res.ok || !res.body) return { status: res.status }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const evt = parseSSE(raw)
        if (!evt) continue // keepalive comments / blank frames
        if (evt.event === 'ready') {
          onEvent({ kind: 'ready' })
        } else if (evt.event === 'message') {
          onEvent({ kind: 'message' })
        } else if (evt.event === 'close') {
          onEvent({ kind: 'close', reason: String(evt.data.reason ?? 'closed') })
          return { status: 200 }
        }
      }
    }
  } catch {
    // The reader throws on abort - a normal reconnect/unmount, not an error.
    return { status: 0 }
  } finally {
    try {
      await reader.cancel()
    } catch {
      /* already closed/errored - nothing to release */
    }
  }
  return { status: 200 }
}

function parseSSE(raw: string): { event: string; data: Record<string, unknown> } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}
