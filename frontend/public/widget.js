/*
 * Agentic RAG — support widget loader (US-083).
 *
 * A buyer embeds this ONE tiny, dependency-free script on their page:
 *
 *   <script
 *     src="https://YOUR-KIT-ORIGIN/widget.js"
 *     data-public-key="wk_pk_xxx"
 *     data-brand-color="#2563eb"
 *     data-position="bottom-right"
 *     async></script>
 *
 * It injects a CROSS-ORIGIN iframe whose `src` is the kit's OWN origin (derived
 * from this script's own URL — never the host origin) and talks to it via
 * `postMessage` only. Because the iframe is a different origin, the conversation
 * token + session it stores in its own `localStorage` are unreadable by the host
 * page's JS (or an XSS on it) — that cross-origin boundary IS the security control
 * (ADR-0008 max-isolation embed).
 *
 * REJECTED ALTERNATIVE — web component / shadow DOM: a shadow-DOM widget shares
 * the host's JavaScript realm, so host JS could read the token straight off the
 * element's state/storage. Shadow-DOM encapsulation is style/markup only, never a
 * trust boundary. We deliberately use a cross-origin iframe instead.
 *
 * This file is plain ES5-ish JS served verbatim from `public/`. The postMessage
 * contract it implements is the source-of-truth TS in src/widget/protocol.ts —
 * keep the channel string and message shapes below in lockstep with it.
 */
(function () {
  'use strict'

  // Must match WIDGET_CHANNEL in src/widget/protocol.ts.
  var CHANNEL = 'ar-support-widget@1'

  // Idempotent: a page that includes the loader twice gets one widget.
  if (window.SupportWidget) return

  // --- locate this script + derive the kit origin --------------------------
  function findSelfScript() {
    if (document.currentScript) return document.currentScript
    // Fallbacks for async/deferred execution where currentScript may be null.
    var byKey = document.querySelector('script[data-public-key]')
    if (byKey) return byKey
    var scripts = document.getElementsByTagName('script')
    for (var i = scripts.length - 1; i >= 0; i--) {
      if (scripts[i].src && scripts[i].src.indexOf('widget.js') !== -1) {
        return scripts[i]
      }
    }
    return null
  }

  var self = findSelfScript()
  if (!self || !self.src) {
    // Without our own URL we cannot find the kit origin to load the iframe from.
    if (window.console) console.error('[support-widget] loader could not locate its own <script> src')
    return
  }

  // The iframe is served from the SAME origin/path as this loader, NOT the host.
  var iframeSrc = new URL('widget.html', self.src).href
  var widgetOrigin = new URL(iframeSrc).origin

  // --- read config (data-* attributes, then optional global override) -------
  function readConfig(script) {
    var d = script.dataset || {}
    var cfg = {
      publicKey: d.publicKey || '',
      // NOTE: no `apiBase` here on purpose. The widget talks only to its own
      // build-time backend (the kit's origin); letting the host point it elsewhere
      // would let a host XSS redirect the conversation token. See protocol.ts.
      position: d.position === 'bottom-left' ? 'bottom-left' : 'bottom-right',
      brandColor: d.brandColor || undefined,
      greeting: d.greeting || undefined,
      title: d.title || undefined,
      launcherIcon: d.launcherIcon || undefined,
    }
    var zRaw = d.zIndex
    cfg.zIndex = zRaw && /^\d+$/.test(zRaw) ? parseInt(zRaw, 10) : 2147483000
    // A global lets buyers configure without editing the <script> tag.
    var override = window.SupportWidgetSettings
    if (override && typeof override === 'object') {
      for (var k in override) {
        if (Object.prototype.hasOwnProperty.call(override, k)) cfg[k] = override[k]
      }
    }
    return cfg
  }

  var config = readConfig(self)
  if (!config.publicKey) {
    if (window.console) console.error('[support-widget] missing data-public-key; widget not loaded')
    return
  }

  var side = config.position === 'bottom-left' ? 'left' : 'right'
  var MARGIN = 16
  // Box sizes (px). Closed = just the launcher bubble + shadow room; open = the
  // panel stacked above the bubble. The iframe only captures pointer events over
  // its own box, so a closed widget never blocks the rest of the host page.
  var CLOSED = { w: 96, h: 96 }
  // OPEN box leaves slack around the 360x540 panel (+launcher) so the in-iframe
  // .sw-root insets (margin 20/24) and the panel's drop-shadow render fully.
  var OPEN = { w: 404, h: 672 }

  // --- inject the cross-origin iframe --------------------------------------
  var iframe = document.createElement('iframe')
  iframe.src = iframeSrc
  iframe.title = 'Support chat'
  iframe.setAttribute('allowtransparency', 'true')
  iframe.setAttribute('aria-hidden', 'false')
  var s = iframe.style
  s.position = 'fixed'
  s.bottom = MARGIN + 'px'
  s[side] = MARGIN + 'px'
  s.width = CLOSED.w + 'px'
  s.height = CLOSED.h + 'px'
  s.maxWidth = 'calc(100vw - ' + 2 * MARGIN + 'px)'
  s.maxHeight = 'calc(100vh - ' + 2 * MARGIN + 'px)'
  s.border = '0'
  s.background = 'transparent'
  s.colorScheme = 'normal'
  s.zIndex = String(config.zIndex)

  function applySize(open) {
    var box = open ? OPEN : CLOSED
    iframe.style.width = box.w + 'px'
    iframe.style.height = box.h + 'px'
  }

  function mount() {
    document.body.appendChild(iframe)
  }
  if (document.body) mount()
  else window.addEventListener('DOMContentLoaded', mount)

  // --- host <-> widget postMessage -----------------------------------------
  var ready = false
  var pending = [] // commands queued before the iframe is ready

  function postToWidget(message) {
    if (!iframe.contentWindow) return
    // targetOrigin pinned to the kit origin — never '*'.
    iframe.contentWindow.postMessage(message, widgetOrigin)
  }

  function sendCommand(command) {
    var msg = { channel: CHANNEL, type: 'command', command: command }
    if (ready) postToWidget(msg)
    else pending.push(msg)
  }

  window.addEventListener('message', function (event) {
    // Only trust messages from OUR iframe's origin and window.
    if (event.origin !== widgetOrigin) return
    if (event.source !== iframe.contentWindow) return
    var data = event.data
    if (!data || data.channel !== CHANNEL || typeof data.type !== 'string') return

    if (data.type === 'ready') {
      ready = true
      postToWidget({ channel: CHANNEL, type: 'init', config: config })
      for (var i = 0; i < pending.length; i++) postToWidget(pending[i])
      pending = []
      return
    }

    if (data.type === 'state') {
      applySize(!!data.open)
      return
    }

    if (data.type === 'unread') {
      var count = typeof data.count === 'number' ? data.count : 0
      // Surface unread state to the host page WITHOUT exposing any widget data —
      // e.g. so the host can update its title/favicon. A CustomEvent + an optional
      // callback; never the token.
      try {
        window.dispatchEvent(
          new CustomEvent('supportwidget:unread', { detail: { count: count } })
        )
      } catch (e) {
        /* CustomEvent constructor unavailable — non-fatal */
      }
      if (typeof window.SupportWidget.onUnread === 'function') {
        window.SupportWidget.onUnread(count)
      }
    }
  })

  // --- host-facing API (token-free by construction) -------------------------
  window.SupportWidget = {
    open: function () {
      sendCommand('open')
    },
    close: function () {
      sendCommand('close')
    },
    toggle: function () {
      sendCommand('toggle')
    },
    /** Optional: host sets this to be notified of unread-count changes. */
    onUnread: null,
  }
})()
