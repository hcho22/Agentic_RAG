import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  WIDGET_CHANNEL,
  isWidgetMessage,
  type WidgetInitConfig,
  type WidgetToHostMessage,
} from './protocol'
import {
  getConversationId,
  getSessionId,
  isEscalationEmailHandled,
  markEscalationEmailHandled,
} from './storage'
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

  const {
    messages,
    sending,
    error,
    throttledUntil,
    status,
    hasConversation,
    escalating,
    send,
    escalate,
  } = useConversation(config.publicKey)

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

          {/* US-091 "talk to a human" + US-092 optional follow-up email, in one panel
              between the transcript and the composer. Offered while ACTIVE (customer-
              initiated) AND after a model-mediated escalate (to still collect an
              email); it self-hides once handled/resolved. */}
          <EscalationControls
            brand={brand}
            publicKey={config.publicKey}
            status={status}
            canEscalate={status === 'active' && hasConversation}
            escalating={escalating}
            onEscalate={escalate}
          />

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

// US-091 "talk to a human" + US-092 optional follow-up email, in one panel.
//
// The email is surfaced ONLY at the escalation moment, NEVER as a pre-chat wall,
// and NEVER gates the handoff (a blank email still escalates). Two entry points,
// one form:
//   * ACTIVE (customer-initiated, US-091): "Talk to a human" reveals the form; the
//     email is optional and "Connect me" escalates with or without it.
//   * ESCALATED (e.g. the bot escalated on its own): the form appears once so the
//     customer can still leave a follow-up email, then steps aside for the
//     MessageList's "a team member will reply here shortly" note.
// v1 sends NO automated email — the address is stored as metadata and shown to the
// agent in the operator queue (US-087) for manual follow-up.
function EscalationControls({
  brand,
  publicKey,
  status,
  canEscalate,
  escalating,
  onEscalate,
}: {
  brand: string
  publicKey: string
  status: string
  canEscalate: boolean
  escalating: boolean
  onEscalate: (email?: string | null) => Promise<'ok' | 'invalid' | 'error'>
}) {
  const [mode, setMode] = useState<'idle' | 'form' | 'done'>('idle')
  const [email, setEmail] = useState('')
  const [emailError, setEmailError] = useState<string | null>(null)
  // Seed "already handled" from storage for the CURRENT conversation so a reload of a
  // still-escalated conversation does not re-nag; best-effort (private-mode safe).
  const [handled, setHandled] = useState(() => {
    const id = getConversationId(publicKey)
    return id ? isEscalationEmailHandled(id) : false
  })

  // When a (new) conversation is active, re-derive the handled flag + reset the form
  // so a prior conversation's "handled" state never suppresses a later escalation's
  // email prompt within one widget mount. Only fires on a transition INTO active, so
  // it never resets a form the customer is mid-way through (status stays 'active').
  useEffect(() => {
    if (status !== 'active') return
    const id = getConversationId(publicKey)
    setHandled(id ? isEscalationEmailHandled(id) : false)
    setMode('idle')
  }, [status, publicKey])

  const markHandled = () => {
    const id = getConversationId(publicKey)
    if (id) markEscalationEmailHandled(id)
    setHandled(true)
  }

  const doEscalate = async (emailArg: string | null) => {
    setEmailError(null)
    const outcome = await onEscalate(emailArg)
    if (outcome === 'ok') {
      markHandled()
      setMode('done')
    } else if (outcome === 'invalid') {
      setEmailError('Please enter a valid email address.')
    }
    // 'error' → the hook surfaced a global error; keep the form open for a retry.
  }

  if (status === 'resolved') return null

  const escalated = status === 'escalated'
  // Nothing to escalate yet (no conversation) and not escalated → render nothing.
  if (!escalated && !canEscalate) return null
  // Escalated + already handled/completed → step aside for the "reply shortly" note.
  if (escalated && (handled || mode === 'done')) return null

  // ACTIVE + not-yet-opened → the "talk to a human" call to action (US-091).
  if (!escalated && mode !== 'form') {
    return (
      <div className="sw-escalate">
        <button
          type="button"
          className="sw-escalate__cta"
          onClick={() => {
            setEmail('')
            setEmailError(null)
            setMode('form')
          }}
        >
          Talk to a human
        </button>
      </div>
    )
  }

  // The optional-email form. ACTIVE → the escalation step itself (email optional,
  // "Connect me"); ESCALATED → collect a follow-up email after the fact ("Send", or
  // skip). The primary action never requires an email except the escalated "Send"
  // (where a blank field is meaningless — use "No thanks" to skip).
  const busy = escalating
  const trimmed = email.trim()
  return (
    <div className="sw-escalate sw-escalate--form">
      <div className="sw-escalate__title">
        {escalated ? 'A team member will follow up' : 'Talk to a human'}
      </div>
      <label className="sw-escalate__label" htmlFor="sw-escalate-email">
        Leave your email and we’ll follow up (optional)
      </label>
      <input
        id="sw-escalate-email"
        className="sw-escalate__input"
        type="email"
        inputMode="email"
        autoComplete="email"
        placeholder="you@example.com"
        value={email}
        disabled={busy}
        onChange={(e) => {
          setEmail(e.target.value)
          if (emailError) setEmailError(null)
        }}
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return
          e.preventDefault()
          if (escalated) {
            if (trimmed) void doEscalate(trimmed)
          } else {
            void doEscalate(trimmed || null)
          }
        }}
      />
      {emailError && <div className="sw-escalate__error">{emailError}</div>}
      <div className="sw-escalate__actions">
        {escalated ? (
          <>
            <button
              type="button"
              className="sw-escalate__secondary"
              onClick={() => {
                markHandled()
                setMode('done')
              }}
              disabled={busy}
            >
              No thanks
            </button>
            <button
              type="button"
              className="sw-escalate__primary"
              style={{ background: brand }}
              onClick={() => void doEscalate(trimmed)}
              disabled={busy || !trimmed}
            >
              {busy ? 'Sending…' : 'Send'}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="sw-escalate__secondary"
              onClick={() => setMode('idle')}
              disabled={busy}
            >
              Not now
            </button>
            <button
              type="button"
              className="sw-escalate__primary"
              style={{ background: brand }}
              onClick={() => void doEscalate(trimmed || null)}
              disabled={busy}
            >
              {busy ? 'Connecting…' : 'Connect me'}
            </button>
          </>
        )}
      </div>
    </div>
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
