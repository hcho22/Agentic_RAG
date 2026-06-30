// US-083: the widget's own-origin persistence.
//
// This module runs ONLY inside the iframe, which is served from the kit's own
// origin. Everything it writes lands in the iframe origin's `localStorage`, which
// the host page (a DIFFERENT origin) cannot read — `host.iframe.contentWindow
// .localStorage` throws a cross-origin `SecurityError`, and the host's own
// `localStorage` is a separate store entirely. This is exactly the isolation the
// cross-origin embed buys (ADR-0008 max-isolation): even an XSS on the host page
// never sees the customer's conversation token.
//
// Keys are namespaced by the non-secret public key so two widgets (two keys) on
// one page never collide. The conversation token itself is the US-071 opaque
// per-conversation token (returned once in the US-078 `X-Conversation-Token`
// response header); US-084 wires the send/resume flow that populates it. The shell
// also mints a stable per-(browser, public key) session id now as client
// continuity/reconnect groundwork, and so the isolation property is verifiable
// today. That id is NOT transmitted on any request and is NOT the backend's
// per-session rate-limit key - US-076 keys its per-session window on the
// XFF-derived client IP (`_widget_client_ip`), not this value.

const SESSION_PREFIX = 'ar-support:session:'
const TOKEN_PREFIX = 'ar-support:token:'
const CONVERSATION_PREFIX = 'ar-support:conversation:'

/** Best-effort localStorage; private-mode / disabled storage degrades to no-op. */
function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSet(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* storage unavailable (private mode, quota) — non-fatal for the shell */
  }
}

function safeRemove(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch {
    /* non-fatal */
  }
}

function randomId(): string {
  // crypto.randomUUID is available in every browser the kit targets; fall back to
  // a timestamp+random composite only if it is somehow absent.
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * A stable per-(browser, public key) session id, minted once and reused across
 * reloads. Stored in the iframe origin so it is unreadable from the host.
 */
export function getSessionId(publicKey: string): string {
  const key = SESSION_PREFIX + publicKey
  const existing = safeGet(key)
  if (existing) return existing
  const fresh = randomId()
  safeSet(key, fresh)
  return fresh
}

export function getConversationToken(publicKey: string): string | null {
  return safeGet(TOKEN_PREFIX + publicKey)
}

export function setConversationToken(publicKey: string, token: string): void {
  safeSet(TOKEN_PREFIX + publicKey, token)
}

/**
 * The conversation id is stored alongside the token so the transcript can be
 * resumed on reload. It is NOT a credential - the transcript endpoint enforces the
 * token↔id binding server-side (US-071), so a stale id simply fails closed (401).
 */
export function getConversationId(publicKey: string): string | null {
  return safeGet(CONVERSATION_PREFIX + publicKey)
}

export function setConversationId(publicKey: string, conversationId: string): void {
  safeSet(CONVERSATION_PREFIX + publicKey, conversationId)
}

/** Clear the conversation token + id together (on resolve / invalid token). */
export function clearConversation(publicKey: string): void {
  safeRemove(TOKEN_PREFIX + publicKey)
  safeRemove(CONVERSATION_PREFIX + publicKey)
}
