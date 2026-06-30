// US-083: the host <-> widget postMessage contract.
//
// The support widget is embedded as a CROSS-ORIGIN iframe served from the kit's
// own origin (NOT the buyer's host origin). That cross-origin boundary is the
// security control: the conversation token + session live in the iframe's
// own-origin `localStorage`, structurally unreadable by the host page's JS (or an
// XSS on it). Because nothing is shared in-process, the ONLY channel between the
// host page (where the loader runs) and the widget (the iframe) is `postMessage`.
//
// A web-component / shadow-DOM embed is DELIBERATELY REJECTED: shadow DOM is the
// SAME JavaScript realm as the host, so host JS could read the token straight off
// the component's state/storage. Encapsulation there is cosmetic (style/markup),
// never a trust boundary. See docs/widget-embed.md.
//
// The vanilla loader (frontend/public/widget.js) cannot import this TS module, so
// it duplicates the `WIDGET_CHANNEL` string and the message shapes by hand. Keep
// the two in lockstep — this file is the source of truth.

/**
 * Channel marker stamped on every message in both directions. Both sides ignore
 * any `MessageEvent` whose `data.channel` does not match, so the widget never
 * acts on unrelated `postMessage` traffic on a busy host page. The `@1` suffix
 * lets a future protocol revision coexist without a silent mismatch.
 */
export const WIDGET_CHANNEL = 'ar-support-widget@1'

/** The non-secret public key + presentation/init config the loader forwards. */
export interface WidgetInitConfig {
  /** US-072 non-secret `wk_pk_…` public key naming which workspace's bot to reach. */
  publicKey: string
  // NOTE: there is deliberately NO host-supplied `apiBase`. The widget always
  // talks to its OWN build-time backend (VITE_BACKEND_URL), which the kit controls.
  // Trusting a host-provided base would let a host-page XSS redirect the US-071
  // conversation token to an attacker server (the iframe sends it in the
  // X-Conversation-Token header) - defeating US-083's isolation. Where the token is
  // sent must never be host-controllable.
  /** Corner the launcher + panel dock to. */
  position?: 'bottom-right' | 'bottom-left'
  /** Theming knobs (US-084): brand color, greeting line, panel title, launcher glyph. */
  brandColor?: string
  greeting?: string
  title?: string
  launcherIcon?: string
}

// --- host (loader) -> widget (iframe) ---------------------------------------

export interface InitMessage {
  channel: typeof WIDGET_CHANNEL
  type: 'init'
  config: WidgetInitConfig
}

/** A host-driven open/close request. The iframe stays the single source of truth. */
export interface CommandMessage {
  channel: typeof WIDGET_CHANNEL
  type: 'command'
  command: 'open' | 'close' | 'toggle'
}

export type HostToWidgetMessage = InitMessage | CommandMessage

// --- widget (iframe) -> host (loader) ---------------------------------------

/** Posted once the iframe's listener is live so the loader knows to send `init`. */
export interface ReadyMessage {
  channel: typeof WIDGET_CHANNEL
  type: 'ready'
}

/** The iframe's authoritative open/closed state; the loader resizes off this. */
export interface StateMessage {
  channel: typeof WIDGET_CHANNEL
  type: 'state'
  open: boolean
}

/** Unread-badge count surfaced to the host (e.g. to update a title/favicon). */
export interface UnreadMessage {
  channel: typeof WIDGET_CHANNEL
  type: 'unread'
  count: number
}

export type WidgetToHostMessage = ReadyMessage | StateMessage | UnreadMessage

/** Narrowing guard for a well-formed message on our channel. */
export function isWidgetMessage(data: unknown): data is { channel: string; type: string } {
  return (
    typeof data === 'object' &&
    data !== null &&
    (data as { channel?: unknown }).channel === WIDGET_CHANNEL &&
    typeof (data as { type?: unknown }).type === 'string'
  )
}
