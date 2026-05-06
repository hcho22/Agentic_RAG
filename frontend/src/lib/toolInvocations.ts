// US-025: derive per-turn tool attribution from the persisted message rows.
//
// The Chat Completions tool-call loop writes intermediate rows for every
// assistant call + tool result so the conversation can rebuild after a page
// refresh. The UI renders one bubble per *answering* assistant message; this
// module folds the intermediate calls/results into that bubble's metadata so
// the badges + expansion panel show what the agent actually did.

import type { MessageRow, PersistedToolCall } from '@/lib/chat'

export type SearchDocumentsResult = {
  id: string
  document_id: string
  chunk_index: number
  content: string
  similarity: number
  filename: string
}

export type SearchDocumentsArgs = {
  query?: string
  top_k?: number
  filters?: unknown
}

export type SearchDocumentsResultPayload = {
  results?: SearchDocumentsResult[]
  count?: number
  retrieval_mode?: string
  reranker?: string
  similarity_threshold?: number
  error?: string
}

export type QueryDatabaseArgs = {
  question?: string
  row_limit?: number
}

export type QueryDatabaseResultPayload = {
  sql?: string
  columns?: string[]
  rows?: Record<string, unknown>[]
  row_count?: number
  truncated?: boolean
  error?: string
}

export type WebSearchArgs = {
  query?: string
  top_k?: number
}

export type WebSearchHit = {
  title: string
  url: string
  snippet: string
}

export type WebSearchResultPayload = {
  results?: WebSearchHit[]
  count?: number
  error?: string
}

export type ToolInvocation =
  | {
      kind: 'search_documents'
      toolCallId: string
      args: SearchDocumentsArgs
      result: SearchDocumentsResultPayload | null
    }
  | {
      kind: 'query_database'
      toolCallId: string
      args: QueryDatabaseArgs
      result: QueryDatabaseResultPayload | null
    }
  | {
      kind: 'web_search'
      toolCallId: string
      args: WebSearchArgs
      result: WebSearchResultPayload | null
    }
  | {
      kind: 'unknown'
      toolCallId: string
      name: string
      args: unknown
      result: unknown
    }

export type RenderItem =
  | { kind: 'user'; message: MessageRow }
  | { kind: 'assistant'; message: MessageRow; invocations: ToolInvocation[] }

function safeParseJSON(raw: string | null): unknown {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function buildInvocation(
  call: PersistedToolCall,
  resultText: string | null,
): ToolInvocation {
  const name = call.function?.name ?? ''
  const args = safeParseJSON(call.function?.arguments ?? null)
  const result = safeParseJSON(resultText)
  if (name === 'search_documents') {
    return {
      kind: 'search_documents',
      toolCallId: call.id,
      args: (args ?? {}) as SearchDocumentsArgs,
      result: (result ?? null) as SearchDocumentsResultPayload | null,
    }
  }
  if (name === 'query_database') {
    return {
      kind: 'query_database',
      toolCallId: call.id,
      args: (args ?? {}) as QueryDatabaseArgs,
      result: (result ?? null) as QueryDatabaseResultPayload | null,
    }
  }
  if (name === 'web_search') {
    return {
      kind: 'web_search',
      toolCallId: call.id,
      args: (args ?? {}) as WebSearchArgs,
      result: (result ?? null) as WebSearchResultPayload | null,
    }
  }
  return {
    kind: 'unknown',
    toolCallId: call.id,
    name,
    args,
    result,
  }
}

// Walks `messages` in chronological order and emits a flat render list with
// tool invocations attached to the assistant turn that *answered* the user.
//
// Intermediate assistant rows (those with tool_calls but no content) are
// dropped from the render list — their tool calls flow forward to the next
// answering assistant message. Tool result rows are matched by tool_call_id
// to the most-recent un-resolved call. Unresolved calls (e.g. an aborted
// turn) still surface in the badges with `result: null` so the user can see
// what the agent attempted.
export function buildRenderItems(messages: MessageRow[]): RenderItem[] {
  const items: RenderItem[] = []
  // Buffered calls awaiting either (a) a matching tool result row or (b) the
  // final assistant content message that closes the turn.
  let pending: { call: PersistedToolCall; result: string | null }[] = []

  for (const m of messages) {
    if (m.role === 'user') {
      // New user turn — drop any leftover unresolved calls; the prior assistant
      // turn must have errored out, but we don't want them to bleed across.
      pending = []
      const userContent = (m.content ?? '').trim()
      if (userContent.length > 0) {
        items.push({ kind: 'user', message: m })
      }
      continue
    }

    if (m.role === 'assistant') {
      if (m.tool_calls && m.tool_calls.length > 0) {
        for (const call of m.tool_calls) {
          pending.push({ call, result: null })
        }
      }
      const content = (m.content ?? '').trim()
      if (content.length > 0) {
        // Answering message — flush pending calls onto it.
        const invocations = pending.map(({ call, result }) =>
          buildInvocation(call, result),
        )
        pending = []
        items.push({ kind: 'assistant', message: m, invocations })
      }
      continue
    }

    if (m.role === 'tool') {
      // Match by tool_call_id (the FK back to the assistant call).
      const slot = pending.find(
        (p) => p.call.id === m.tool_call_id && p.result === null,
      )
      if (slot) slot.result = m.content
      continue
    }

    // System / unknown roles are not surfaced in the chat transcript.
  }

  return items
}

// Convenience: count of distinct tool kinds in an invocation list. Used by
// MessageList to decide whether to render the badges row at all.
export function hasToolInvocations(invocations: ToolInvocation[]): boolean {
  return invocations.length > 0
}
