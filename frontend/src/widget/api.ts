// US-084: the widget's backend client, scoped to the anonymous public surface.
//
// Two endpoints, both authed by the US-071 opaque per-conversation token (NOT a
// Supabase JWT - the anonymous customer is off the Supabase trust surface):
//
//   * POST /widget/conversations/messages - the customer turn. With no token it is
//     the FIRST message (US-078 lazily creates the conversation and returns the raw
//     token ONCE in the `X-Conversation-Token` RESPONSE HEADER); with the token it
//     RESUMES. The response is a request-scoped SSE the bot answer streams over as
//     `conversation` / `delta` / `done` / `error` events (US-079) - the SAME shape
//     /api/chat uses.
//   * GET /widget/conversations/{id}/transcript - the durable message history
//     (US-071), used to recover prior messages on open AND, until US-081 ships the
//     live customer-SSE push, to surface agent replies via a light poll.
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
