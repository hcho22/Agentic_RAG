import { supabase } from '@/lib/supabase'

export type ThreadRow = {
  id: string
  user_id: string
  title: string | null
  created_at: string
  openai_thread_id?: string | null
}

export type MessageRow = {
  id: string
  thread_id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  created_at: string
}

export const TITLE_MAX_LEN = 50

export async function listThreads(): Promise<ThreadRow[]> {
  const { data, error } = await supabase
    .from('threads')
    .select('id, user_id, title, created_at, openai_thread_id')
    .order('created_at', { ascending: false })
  if (error) throw error
  return (data ?? []) as ThreadRow[]
}

export async function createThread(userId: string): Promise<ThreadRow> {
  const { data, error } = await supabase
    .from('threads')
    .insert({ user_id: userId, title: null })
    .select('id, user_id, title, created_at, openai_thread_id')
    .single()
  if (error) throw error
  return data as ThreadRow
}

export async function listMessages(threadId: string): Promise<MessageRow[]> {
  const { data, error } = await supabase
    .from('messages')
    .select('id, thread_id, role, content, created_at')
    .eq('thread_id', threadId)
    .order('created_at', { ascending: true })
  if (error) throw error
  return (data ?? []) as MessageRow[]
}

export async function updateThreadTitle(threadId: string, title: string): Promise<void> {
  const { error } = await supabase.from('threads').update({ title }).eq('id', threadId)
  if (error) throw error
}

export function deriveTitle(firstUserMessage: string): string {
  const trimmed = firstUserMessage.trim().replace(/\s+/g, ' ')
  return trimmed.length > TITLE_MAX_LEN ? trimmed.slice(0, TITLE_MAX_LEN) : trimmed
}

const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '')

export type ChatStreamEvent =
  | { kind: 'delta'; text: string }
  | { kind: 'done'; messageId: string; responseId: string | null }
  | { kind: 'error'; message: string }

// Stream a chat turn from the backend /api/chat SSE endpoint.
// Backend persists both the user and assistant messages via RLS using the
// caller's JWT, so we don't double-write from the client.
export async function* streamChatTurn(
  threadId: string,
  message: string,
): AsyncGenerator<ChatStreamEvent> {
  const { data: sess } = await supabase.auth.getSession()
  const token = sess.session?.access_token
  if (!token) {
    yield { kind: 'error', message: 'Not signed in.' }
    return
  }

  const res = await fetch(`${BACKEND_URL}/api/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({ thread_id: threadId, message }),
  })

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '')
    yield { kind: 'error', message: text || `Request failed (${res.status})` }
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // Split on blank-line SSE record separator.
    let sepIndex: number
    while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      const evt = parseSSE(raw)
      if (!evt) continue
      if (evt.event === 'delta' && typeof evt.data.text === 'string') {
        yield { kind: 'delta', text: evt.data.text }
      } else if (evt.event === 'done') {
        yield {
          kind: 'done',
          messageId: String(evt.data.message_id ?? ''),
          responseId: evt.data.response_id ? String(evt.data.response_id) : null,
        }
      } else if (evt.event === 'error') {
        yield { kind: 'error', message: String(evt.data.message ?? 'Unknown error') }
      }
    }
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
