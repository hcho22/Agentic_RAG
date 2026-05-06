import { useEffect, useMemo, useRef } from 'react'
import { cn } from '@/lib/utils'
import type { MessageRow } from '@/lib/chat'
import { ToolAttribution } from '@/components/chat/ToolAttribution'
import { buildRenderItems, type ToolInvocation } from '@/lib/toolInvocations'

type StreamingMessage = { role: 'assistant'; content: string }

type Props = {
  messages: MessageRow[]
  streaming: StreamingMessage | null
  emptyHint?: string
}

export function MessageList({ messages, streaming, emptyHint }: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // US-025: fold tool_calls + tool result rows into the answering assistant
  // turn so we can render badges below the bubble. Memoised so toggling a
  // panel doesn't re-walk the full transcript.
  const renderItems = useMemo(() => buildRenderItems(messages), [messages])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [renderItems, streaming?.content])

  if (renderItems.length === 0 && !streaming) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-neutral-500">
        {emptyHint ?? 'Send a message to start the conversation.'}
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        {renderItems.map((item) =>
          item.kind === 'user' ? (
            <MessageBubble
              key={item.message.id}
              role="user"
              content={item.message.content ?? ''}
            />
          ) : (
            <AssistantTurn
              key={item.message.id}
              content={item.message.content ?? ''}
              invocations={item.invocations}
            />
          ),
        )}
        {streaming && <MessageBubble role="assistant" content={streaming.content} streaming />}
        <div ref={endRef} />
      </div>
    </div>
  )
}

function AssistantTurn({
  content,
  invocations,
}: {
  content: string
  invocations: ToolInvocation[]
}) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[80%]">
        <div className="whitespace-pre-wrap rounded-lg bg-neutral-800 px-4 py-2 text-sm text-neutral-100">
          {content}
        </div>
        <ToolAttribution invocations={invocations} />
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
