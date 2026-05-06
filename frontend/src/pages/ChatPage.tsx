import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'
import { useToast } from '@/components/ui/toast'
import { AppHeader } from '@/components/AppHeader'
import { ThreadList } from '@/components/chat/ThreadList'
import { MessageList } from '@/components/chat/MessageList'
import { ChatInput } from '@/components/chat/ChatInput'
import { ChatModeToggle } from '@/components/chat/ChatModeToggle'
import {
  createThread,
  deriveTitle,
  fetchBackendConfig,
  listMessages,
  listThreads,
  streamChatTurn,
  updateThreadTitle,
  type ChatMode,
  type MessageRow,
  type ThreadRow,
} from '@/lib/chat'

export function ChatPage() {
  const { user } = useAuth()
  const { toast } = useToast()
  const navigate = useNavigate()
  const { threadId } = useParams<{ threadId?: string }>()

  const [threads, setThreads] = useState<ThreadRow[]>([])
  const [threadsLoading, setThreadsLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  const [messages, setMessages] = useState<MessageRow[]>([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [streamingContent, setStreamingContent] = useState<string | null>(null)
  const [sending, setSending] = useState(false)

  const [mode, setMode] = useState<ChatMode>('responses')

  useEffect(() => {
    let cancelled = false
    fetchBackendConfig()
      .then((cfg) => {
        if (!cancelled) setMode(cfg.default_chat_mode)
      })
      .catch(() => {
        // Non-fatal: fall back to the UI's default. Backend still honours
        // req.mode and its own CHAT_MODE_DEFAULT when mode is omitted.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refreshThreads = useCallback(async () => {
    setThreadsLoading(true)
    try {
      const rows = await listThreads()
      setThreads(rows)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load threads', 'error')
    } finally {
      setThreadsLoading(false)
    }
  }, [toast])

  useEffect(() => {
    void refreshThreads()
  }, [refreshThreads])

  useEffect(() => {
    if (!threadId) {
      setMessages([])
      return
    }
    let cancelled = false
    setMessagesLoading(true)
    listMessages(threadId)
      .then((rows) => {
        if (!cancelled) setMessages(rows)
      })
      .catch((e) => {
        if (!cancelled) toast(e instanceof Error ? e.message : 'Failed to load messages', 'error')
      })
      .finally(() => {
        if (!cancelled) setMessagesLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [threadId, toast])

  async function handleNewThread() {
    if (!user) return
    setCreating(true)
    try {
      const t = await createThread(user.id)
      setThreads((prev) => [t, ...prev])
      navigate(`/chat/${t.id}`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to create thread', 'error')
    } finally {
      setCreating(false)
    }
  }

  async function handleSend(text: string) {
    if (!user) return
    setSending(true)
    try {
      let activeId = threadId
      let isFirstUserMessage = false

      if (!activeId) {
        const t = await createThread(user.id)
        setThreads((prev) => [t, ...prev])
        activeId = t.id
        isFirstUserMessage = true
        navigate(`/chat/${t.id}`, { replace: true })
      } else {
        isFirstUserMessage = !messages.some((m) => m.role === 'user')
      }

      // Optimistic user message — backend also persists it authoritatively.
      const optimisticUser: MessageRow = {
        id: `optimistic-${Date.now()}`,
        thread_id: activeId,
        role: 'user',
        content: text,
        created_at: new Date().toISOString(),
        tool_calls: null,
        tool_call_id: null,
        name: null,
      }
      setMessages((prev) => [...prev, optimisticUser])

      if (isFirstUserMessage) {
        const title = deriveTitle(text)
        await updateThreadTitle(activeId, title).catch(() => {
          // non-fatal; the row still exists without a title
        })
        setThreads((prev) => prev.map((t) => (t.id === activeId ? { ...t, title } : t)))
      }

      let acc = ''
      setStreamingContent('')
      let gotError = false
      for await (const evt of streamChatTurn(activeId, text, mode)) {
        if (evt.kind === 'delta') {
          acc += evt.text
          setStreamingContent(acc)
        } else if (evt.kind === 'error') {
          gotError = true
          toast(evt.message, 'error')
          break
        } else if (evt.kind === 'done') {
          // Backend persisted both messages; refresh from source of truth.
          const rows = await listMessages(activeId)
          setMessages(rows)
        }
      }
      setStreamingContent(null)
      if (gotError) {
        // Roll back the optimistic user bubble if the turn failed early.
        setMessages((prev) => prev.filter((m) => m.id !== optimisticUser.id))
      }
    } catch (e) {
      setStreamingContent(null)
      toast(e instanceof Error ? e.message : 'Failed to send message', 'error')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <AppHeader />
      <div className="flex min-h-0 flex-1">
        <ThreadList
          threads={threads}
          loading={threadsLoading}
          onNewThread={handleNewThread}
          creating={creating}
        />
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center justify-end border-b border-neutral-800 px-4 py-2">
            <ChatModeToggle mode={mode} onChange={setMode} disabled={sending} />
          </div>
          {messagesLoading ? (
            <div className="flex flex-1 items-center justify-center text-sm text-neutral-500">
              Loading conversation…
            </div>
          ) : (
            <MessageList
              messages={messages}
              streaming={streamingContent !== null ? { role: 'assistant', content: streamingContent } : null}
              emptyHint={
                threadId
                  ? 'Send a message to start the conversation.'
                  : 'Start a new thread or pick one from the sidebar.'
              }
            />
          )}
          <ChatInput onSubmit={handleSend} disabled={sending} />
        </main>
      </div>
    </div>
  )
}
