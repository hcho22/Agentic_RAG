import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  WIDGET_CHANNEL,
  isWidgetMessage,
  type WidgetInitConfig,
  type WidgetToHostMessage,
} from './protocol'
import { getSessionId } from './storage'
import { useConversation, type WidgetMessage } from './useConversation'

// US-083 + US-084: the cross-origin iframe support widget.
//
// US-083 is the embed shell - the cross-origin iframe, the host<->widget
// postMessage handshake, own-origin token/session storage, launcher open/close.
// US-084 is the in-iframe chat UI + theming rendered here: a message list that
// renders the US-079 streamed `delta` answer, a composer (disabled while a turn is
// in flight or throttled), and theming (brand color, position, greeting, launcher
// icon, title) applied ENTIRELY from the loader's `init` config - no host CSS can
// reach this iframe.
//
// Launcher AND panel live INSIDE this iframe; the loader only sizes/positions it
// and relays messages.

const DEFAULT_TITLE = 'Support'
const DEFAULT_GREETING = 'Hi! How can we help?'
const DEFAULT_BRAND = '#2563eb'
const DEFAULT_LAUNCHER_ICON = '💬'

export function WidgetApp() {
  const [config, setConfig] = useState<WidgetInitConfig | null>(null)
  const [open, setOpen] = useState(false)

  // The host origin is learned from the FIRST `init` message and is the ONLY origin
  // we post back to (the `ready` ping is the sole '*' send and carries no secret).
  const hostOriginRef = useRef<string | null>(null)
  // Config is accepted EXACTLY ONCE. Re-init is ignored as defense-in-depth: a
  // host-page XSS is same-origin (so it passes the origin/source checks), and
  // re-applying its config could otherwise let it mutate the widget after setup.
  const initializedRef = useRef(false)

  const postToHost = useCallback((message: WidgetToHostMessage) => {
    const target = hostOriginRef.current
    if (!target) return
    window.parent.postMessage(message, target)
  }, [])

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.source !== window.parent) return
      if (!isWidgetMessage(event.data)) return
      const known = hostOriginRef.current
      if (known !== null && event.origin !== known) return

      const data = event.data as { type: string }
      if (data.type === 'init') {
        if (initializedRef.current) return // accept the first init only
        const incoming = event.data as { config?: WidgetInitConfig }
        if (!incoming.config || typeof incoming.config.publicKey !== 'string') return
        initializedRef.current = true
        hostOriginRef.current = event.origin
        getSessionId(incoming.config.publicKey)
        setConfig(incoming.config)
        return
      }
      if (data.type === 'command') {
        const cmd = (event.data as { command?: string }).command
        if (cmd === 'open') setOpen(true)
        else if (cmd === 'close') setOpen(false)
        else if (cmd === 'toggle') setOpen((v) => !v)
      }
    }

    window.addEventListener('message', onMessage)
    window.parent.postMessage({ channel: WIDGET_CHANNEL, type: 'ready' }, '*')
    return () => window.removeEventListener('message', onMessage)
  }, [])

  // The iframe owns open/closed state; tell the loader so it resizes the iframe.
  useEffect(() => {
    if (!config) return
    postToHost({ channel: WIDGET_CHANNEL, type: 'state', open })
  }, [open, config, postToHost])

  if (!config) {
    // Pre-init: render only the launcher placeholder so the iframe paints nothing
    // until the host config arrives.
    return null
  }

  return <WidgetSurface config={config} open={open} setOpen={setOpen} postToHost={postToHost} />
}

function WidgetSurface({
  config,
  open,
  setOpen,
  postToHost,
}: {
  config: WidgetInitConfig
  open: boolean
  setOpen: (v: boolean | ((p: boolean) => boolean)) => void
  postToHost: (m: WidgetToHostMessage) => void
}) {
  const brand = config.brandColor || DEFAULT_BRAND
  const title = config.title || DEFAULT_TITLE
  const greeting = config.greeting || DEFAULT_GREETING
  const launcherIcon = config.launcherIcon || DEFAULT_LAUNCHER_ICON
  const side = config.position === 'bottom-left' ? 'left' : 'right'

  const { messages, sending, error, throttledUntil, status, send } = useConversation(
    config.publicKey,
  )

  // --- unread badge (surfaced to the host + shown on the launcher) ----------
  // `readThrough` is how far the user has seen; while open everything is read, so a
  // support (assistant) message arriving while CLOSED accrues unread.
  const [readThrough, setReadThrough] = useState(0)
  const initializedRef = useRef(false)
  useEffect(() => {
    if (open) {
      setReadThrough(messages.length)
      return
    }
    if (!initializedRef.current && messages.length > 0) {
      // Historical messages recovered on first load are not "unread".
      initializedRef.current = true
      setReadThrough(messages.length)
    }
  }, [open, messages.length])

  const unread = open
    ? 0
    : messages.slice(readThrough).filter((m) => m.role === 'assistant').length

  useEffect(() => {
    postToHost({ channel: WIDGET_CHANNEL, type: 'unread', count: unread })
  }, [unread, postToHost])

  // --- throttle countdown (US-076 429) --------------------------------------
  // Reset the clock the instant throttling begins (layout effect → before paint)
  // so the first rendered countdown is the real Retry-After, not a stale tick.
  const [nowTick, setNowTick] = useState(() => Date.now())
  useLayoutEffect(() => {
    if (!throttledUntil) return
    setNowTick(Date.now())
    const t = setInterval(() => setNowTick(Date.now()), 500)
    return () => clearInterval(t)
  }, [throttledUntil])
  const throttleSecondsLeft = throttledUntil
    ? Math.max(0, Math.ceil((throttledUntil - nowTick) / 1000))
    : 0
  const throttled = throttleSecondsLeft > 0
  // `status === 'resolved'` is currently unreachable on the customer side BY DESIGN:
  // US-071 purges the conversation token on resolve, so a resolved conversation's
  // token returns an opaque 401 that the client treats as a dead token (clear +
  // silent fresh start). The resolved-disabled UX below is retained as defensive
  // scaffolding for future flows (US-081 live status push / US-088 agent resolve).
  const resolved = status === 'resolved'

  return (
    <div className={`sw-root sw-${side}`} data-open={open}>
      {open && (
        <section className="sw-panel" role="dialog" aria-label={title}>
          <header className="sw-panel__header" style={{ background: brand }}>
            <span className="sw-panel__title">{title}</span>
            <button
              type="button"
              className="sw-panel__close"
              aria-label="Close support chat"
              onClick={() => setOpen(false)}
            >
              ×
            </button>
          </header>

          <MessageList greeting={greeting} brand={brand} messages={messages} status={status} />

          {error && <div className="sw-error">{error}</div>}

          <Composer
            brand={brand}
            disabled={sending || resolved}
            throttled={throttled}
            throttleSecondsLeft={throttleSecondsLeft}
            resolved={resolved}
            onSend={send}
          />
        </section>
      )}

      <button
        type="button"
        className="sw-launcher"
        style={{ background: brand }}
        aria-label={open ? 'Close support chat' : 'Open support chat'}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">{open ? '×' : launcherIcon}</span>
        {!open && unread > 0 && (
          <span className="sw-badge" aria-label={`${unread} unread`}>
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>
    </div>
  )
}

function MessageList({
  greeting,
  brand,
  messages,
  status,
}: {
  greeting: string
  brand: string
  messages: WidgetMessage[]
  status: string
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const seenIdsRef = useRef<Set<string>>(new Set())

  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // Auto-scroll only when the user just sent a message or is already at the bottom,
  // so the 8s transcript poll (which swaps the messages array even when the content
  // is unchanged) never yanks a reader who scrolled up to read earlier history.
  useEffect(() => {
    const userAppended = messages.some(
      (m) => m.role === 'user' && !seenIdsRef.current.has(m.id),
    )
    seenIdsRef.current = new Set(messages.map((m) => m.id))
    if (!userAppended && !atBottomRef.current) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  return (
    <div className="sw-messages" ref={scrollRef} onScroll={handleScroll}>
      <div className="sw-msg sw-msg--bot">
        <div className="sw-bubble sw-bubble--bot">{greeting}</div>
      </div>

      {messages.map((m) => (
        <div
          key={m.id}
          className={`sw-msg ${m.role === 'user' ? 'sw-msg--user' : 'sw-msg--bot'}`}
        >
          <div
            className={`sw-bubble ${
              m.role === 'user' ? 'sw-bubble--user' : 'sw-bubble--bot'
            } ${m.error ? 'sw-bubble--error' : ''}`}
            style={m.role === 'user' ? { background: brand } : undefined}
          >
            {m.content || (m.streaming ? <TypingDots /> : '')}
          </div>
        </div>
      ))}

      {status === 'escalated' && (
        <div className="sw-system">A team member will reply here shortly.</div>
      )}
    </div>
  )
}

function TypingDots() {
  return (
    <span className="sw-typing" aria-label="typing">
      <span />
      <span />
      <span />
    </span>
  )
}

function Composer({
  brand,
  disabled,
  throttled,
  throttleSecondsLeft,
  resolved,
  onSend,
}: {
  brand: string
  disabled: boolean
  throttled: boolean
  throttleSecondsLeft: number
  resolved: boolean
  onSend: (text: string) => void
}) {
  const [value, setValue] = useState('')
  const blocked = disabled || throttled || resolved

  const submit = () => {
    const text = value.trim()
    if (!text || blocked) return
    onSend(text)
    setValue('')
  }

  return (
    <div className="sw-composer">
      {throttled && (
        <div className="sw-composer__hint">
          Too many messages - try again in {throttleSecondsLeft}s.
        </div>
      )}
      {resolved && (
        <div className="sw-composer__hint">This conversation has been resolved.</div>
      )}
      <div className="sw-composer__row">
        <textarea
          className="sw-composer__input"
          rows={1}
          placeholder={resolved ? 'Conversation ended' : 'Type a message…'}
          value={value}
          disabled={blocked}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
        />
        <button
          type="button"
          className="sw-composer__send"
          style={{ background: brand }}
          aria-label="Send message"
          disabled={blocked || !value.trim()}
          onClick={submit}
        >
          ➤
        </button>
      </div>
    </div>
  )
}
