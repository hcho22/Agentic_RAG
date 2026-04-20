import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import type { MessageRow } from '@/lib/chat'

type StreamingMessage = { role: 'assistant'; content: string }

type Props = {
  messages: MessageRow[]
  streaming: StreamingMessage | null
  emptyHint?: string
}

export function MessageList({ messages, streaming, emptyHint }: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, streaming?.content])

  // US-012: persisted assistant rows that only emitted tool_calls have null /
  // empty content — those are trace fidelity only and shouldn't render as
  // empty bubbles. Tool rows carry raw JSON payloads and are also hidden.
  const visible = messages.filter(
    (m) => (m.role === 'user' || m.role === 'assistant') && (m.content ?? '').trim().length > 0,
  )

  if (visible.length === 0 && !streaming) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-neutral-500">
        {emptyHint ?? 'Send a message to start the conversation.'}
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        {visible.map((m) => (
          <MessageBubble key={m.id} role={m.role as 'user' | 'assistant'} content={m.content ?? ''} />
        ))}
        {streaming && <MessageBubble role="assistant" content={streaming.content} streaming />}
        <div ref={endRef} />
      </div>
    </div>
  )
}

function MessageBubble({
  role,
  content,
  streaming,
}: {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}) {
  const isUser = role === 'user'
  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'max-w-[80%] whitespace-pre-wrap rounded-lg px-4 py-2 text-sm',
          isUser ? 'bg-neutral-100 text-neutral-900' : 'bg-neutral-800 text-neutral-100',
        )}
      >
        {content}
        {streaming && <span className="ml-0.5 inline-block animate-pulse">▍</span>}
      </div>
    </div>
  )
}
