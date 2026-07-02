# Embedding the support widget (US-083)

The support widget is embedded with a tiny loader `<script>` that injects a
**cross-origin iframe served from the kit's own origin** and talks to the host
page via `postMessage` only.
This document explains the embed architecture, the host↔widget message contract,
and - importantly - why a web-component / shadow-DOM embed was **rejected**.

## How a buyer embeds it

```html
<script
  src="https://YOUR-KIT-ORIGIN/widget.js"
  data-public-key="wk_pk_xxx"
  data-brand-color="#2563eb"
  data-greeting="Hi! Ask us anything."
  data-title="Acme Support"
  data-position="bottom-right"
  async></script>
```

`data-public-key` is the **non-secret** US-072 public key (`wk_pk_…`); it only
names which workspace's bot the widget speaks to and grants nothing on its own.
Buyers may instead set a `window.SupportWidgetSettings` object before the script
loads; its fields override the `data-*` attributes.

The loader exposes a small, **token-free** host API:

```js
window.SupportWidget.open()
window.SupportWidget.close()
window.SupportWidget.toggle()
window.SupportWidget.onUnread = (count) => { /* e.g. update the tab title */ }
// also: window.addEventListener('supportwidget:unread', (e) => e.detail.count)
```

## The three origins

| Origin | What runs there | Trust |
| --- | --- | --- |
| **Host origin** (`https://buyer.com`) | the loader (`widget.js`) + the buyer's own page JS | untrusted w.r.t. the widget's token |
| **Kit/widget origin** (`https://kit`) | the iframe shell (`widget.html`), where the US-071 conversation token + session live in `localStorage` | the isolation boundary |
| **Backend API origin** | the `/widget/*` endpoints (US-074 widget CORS) | the hard security boundary |

The loader derives the kit origin from **its own `<script src>`** (not the host
origin), so the iframe `src` is always the kit's `widget.html`. Because the iframe
is a different origin from the host page, the host's JS - or an XSS on it - cannot
read the iframe's `localStorage` (a cross-origin `contentWindow.localStorage`
access throws `SecurityError`) and cannot reach into its `document`/`window`. That
cross-origin boundary **is** the embed's security control (ADR-0008 max-isolation).

> The token's hard protection is the cross-origin boundary plus the US-071/074
> backend gates - not the loader. The loader is the convenience that sets it up.

## Host ↔ widget message contract

All communication is `postMessage`; nothing is shared in-process. Every message
carries `channel: "ar-support-widget@1"`; both sides ignore anything else. The
contract is defined once in [`frontend/src/widget/protocol.ts`](../frontend/src/widget/protocol.ts)
(the vanilla loader hand-duplicates the same strings and shapes).

- **host → widget**: `init` (the public key + presentation config, sent in
  response to `ready`, `targetOrigin` pinned to the kit origin), and `command`
  (`open` / `close` / `toggle`).
- **widget → host**: `ready` (the only `targetOrigin: '*'` send - it carries no
  secret), `state` (`{ open }`, the iframe is the single source of truth; the
  loader resizes the iframe between bubble- and panel-sized off this), and
  `unread` (`{ count }`).

Origin handling is strict in both directions: the loader only acts on messages
whose `event.origin` is the kit origin **and** `event.source` is the iframe's
window; the iframe pins the host origin from the first `init` and refuses any
later message from a different origin. Theming (brand color, greeting, position,
title) flows **only** through `init` - host CSS never reaches the iframe.

## Why NOT a web component / shadow DOM

A shadow-DOM embed was **deliberately rejected**. Shadow DOM encapsulates *markup
and styles*, but the component still runs in the **host page's JavaScript realm**:
host JS (or an XSS on it) can walk the element, read its state, and read whatever
storage it uses - including the customer's conversation token. Its encapsulation
is cosmetic, never a trust boundary.

A **cross-origin iframe** puts the widget in a separate origin with a separate
`localStorage` and an opaque `contentWindow`, so the token is structurally
unreachable from the host. That is the whole point of this story, so the iframe -
not shadow DOM - is the embed. (See the rejection note in `widget.js` and
`protocol.ts`.)

## Local verification (cross-origin)

The security property only holds when the host page is a **different origin** from
the kit, so verify with two servers:

```bash
# terminal 1 — the kit origin (serves widget.js + widget.html)
cd frontend && npm run dev                         # http://localhost:5173

# terminal 2 — a host origin (a DIFFERENT origin)
cd frontend && python3 -m http.server 8099
# open http://localhost:8099/widget-host-example.html
```

`frontend/widget-host-example.html` is the host-page fixture; its "Try to read the
iframe's localStorage / token" button runs the security probe from host JS
(expected: `SecurityError`; the token never appears in host storage or on
`window.SupportWidget`).

## Chat UI + theming (US-084)

The in-iframe chat UI is rendered entirely inside the cross-origin iframe and
themed ONLY from the loader's `init` config - host CSS cannot reach it.

- **Theming knobs** (all from `init`, all optional): `brandColor` (launcher,
  header, user bubbles, send button), `position` (`bottom-right` | `bottom-left`),
  `greeting` (first bot line), `title` (panel header), `launcherIcon` (the closed
  launcher glyph). The loader reads them from `data-*` attributes (or
  `window.SupportWidgetSettings`) and forwards them in `init`.
- **Sending** posts to `POST /widget/conversations/messages` and renders the
  US-079 `delta` stream into an optimistic assistant bubble; the first reply mints
  the US-071 token (read from the `X-Conversation-Token` response header) and the
  durable transcript (US-071) reconciles the optimistic rows.
- **Composer disabling**: while a turn is in flight, while throttled (a US-076
  `429` - the precise `Retry-After` is read cross-origin because the widget CORS
  posture now exposes it; otherwise a conservative default), and when the
  conversation is `resolved`.
- **Unread badge**: when the panel is CLOSED and a support (assistant) message
  arrives, the launcher shows a count badge and the count is surfaced to the host
  via a `unread` postMessage (so the host can update its tab title/favicon);
  opening the panel resets it.
- **Expired/resolved token recovery**: a `401` on send transparently restarts the
  same message as a fresh conversation; a `401` on the transcript clears the dead
  token and stops the poll.

### Live agent replies - the customer SSE push (US-081)

Human agent replies (US-082 writes them to `conversation_messages`) are pushed to
the customer over a long-lived backend SSE,
`GET /widget/conversations/{id}/events`, authorized by the US-071 opaque token (a
plain fetch-based SSE - the customer never holds a Supabase Realtime channel).
`useConversation` holds that stream open whenever a conversation + token exist; an
`event: message` is a low-latency NUDGE that feeds the same
`useConversation.refreshTranscript` seam, which re-reads the durable US-071
transcript (the source of truth). The transcript **poll is retained as a backstop**,
so a dropped/closed SSE never loses a reply - the SSE just makes replies feel
instant. A single backend process needs nothing more (the in-process
`ConversationFanout` registry delivers); a horizontally-scaled deployment sets
`WIDGET_FANOUT_DATABASE_URL` so the `ConversationBridge` carries a reply between
instances over Postgres `LISTEN/NOTIFY` (no Redis/queue infra), and the SSE emits
`event: close` when the conversation is resolved (its token purged).

## Known integration consideration - key resolution + the iframe's `Origin` vs the per-key allowlist

This implementation defers **all** widget-key resolution to the **first message**
(US-078 re-resolve, sent from the iframe = kit origin).
The loader does NOT call `POST /widget/keys/resolve`: it only injects the iframe
and relays postMessage, and the iframe itself only fetches
`/widget/conversations/messages` (on send) and the transcript (poll).
Three consequences compound here, to reconcile before production:

- **No early key validation.**
  Because resolution happens lazily on the first message, a revoked or typo'd key
  presents a fully working-looking widget that only fails when the user actually
  sends - there is no resolve-on-open to catch a bad key up front.
- **US-072's intended loader resolve-on-open is NOT yet wired.**
  Validating the key on the buyer host origin at load time (the buyer origin is the
  one registered in the allowlist) is the missing piece.
- **The iframe's `Origin` is the kit origin, not the buyer's.**
  The iframe is served from the kit origin, so every API call it makes carries
  `Origin: <kit-origin>`, NOT the buyer's host origin. The US-072/073 per-key origin
  allowlist (`widget_keys.allowed_origins`) registers **buyer host origins** and is
  re-checked server-side on the first message (US-078) and in the US-074 widget
  CORS posture. A cross-origin iframe can never present the buyer's origin on these
  calls.

Before production, reconcile the origin/resolution architecture: wire the loader's
resolve-on-open for early buyer-origin validation, AND admit the iframe's kit origin
in the US-074 widget CORS + US-078's first-message origin gate - e.g. by registering
the kit origin, or by gating the iframe's token-authenticated calls by the opaque
US-071 token alone (the real boundary) rather than by `Origin`. This is a
pre-existing cross-cutting concern between the embed model (US-083) and the origin
gate (US-073/078), surfaced by wiring the real client; it is **not** introduced by
US-084 and is left as a follow-up design decision.

## Admin settings — issuing keys, enabling support, share-to-bot (US-090)

Widget keys are issued, rotated, and revoked by a **workspace admin** on the
`/support/settings` route (added to `App.tsx`, reachable from the admin-only
"Support settings" nav link).
This is the ADMIN surface — ROLE-gated (`workspace_membership.role='admin'`), the
one place `role` legitimately enters the trust model (ADR-0002) — and is
deliberately distinct from the MEMBERSHIP-gated `/support/queue` handoff list: a
plain member reaches the queue but is shown an "admins only" note on settings.
The UI gate is cosmetic; the hard boundary is the `widget_keys` admin RLS enforced
server-side under the caller's own JWT (US-072), so a non-admin's issue is
rejected by Postgres, their key list reads back empty, and their revoke matches
zero rows.

- **Enabling support = issuing the first key.**
  There is no separate "enable support" toggle: issuing a workspace's first widget
  key lazily provisions its support bot (US-069, `_ensure_workspace_bot`,
  best-effort), so the settings page frames "no keys yet" as "issue your first key
  to enable support" and treats "≥1 key ever issued" as "support enabled".
- **Rotation is issue-new + revoke-old**, composed client-side (there is no atomic
  rotate endpoint by design — the schema allows multiple keys per workspace so a
  revoked key lingers beside its replacement for audit).
  The page issues the replacement FIRST (a failure leaves the old key untouched
  and the embedded loader still working), then revokes the old one; if the revoke
  leg fails the new key is kept and the admin is told to revoke the old one
  manually rather than lose the replacement.
- **Origins are surfaced with the fail-closed reminder + the `*` dev-only warning.**
  A key with no registered origin resolves nothing (US-073 fail-closed), so the
  issue/rotate form disables submit until at least one non-blank origin is entered
  (mirroring the backend 400), and both the form and each key card flag a `*`
  wildcard entry as dev-only/never-production.
- **Share-to-bot is managed here for the documents the admin OWNS.**
  `listDocuments` reads under owner-only RLS and publish/unpublish are owner-gated
  (US-086), so the section lists exactly the admin's own ready documents with a
  loud, explicitly-confirmed publish action ("answerable to anyone who can reach
  your public widget"); it reuses the same US-086 endpoints as the doc share
  dialog.

**Per-key theming defaults — deferred, and why.**
The AC mentions issuing keys with "theming defaults", but the shipped theming
model is **loader-driven**: the buyer sets brand color / greeting / title /
position / launcher icon as `data-*` attributes on the embed snippet, applied via
the iframe `init` config (US-084).
`widget_keys` has no theming columns and the public resolve path returns none, so
storing per-key server-side theming defaults would be a separate cross-cutting
change spanning the schema, the US-078 resolve payload, and the US-083/084 loader
+ iframe — touching already-shipped public surfaces the US-090 validation test
does not exercise.
The settings page therefore surfaces theming where it actually lives today — on
the copyable embed snippet, which documents the optional theming `data-*`
attributes — rather than persisting per-key defaults; a server-side theming store
is left as a deliberate follow-up.

## Scope

US-083 ships the **shell** (loader, cross-origin iframe, postMessage handshake,
own-origin storage, launcher). US-084 ships the in-iframe **chat UI + theming**
(message list, composer, streamed `delta` rendering, unread badge). US-081 ships the
live customer-SSE **push** (`GET /widget/conversations/{id}/events` + the
`subscribeConversationEvents` consumer feeding `refreshTranscript`) and the optional
multi-instance `LISTEN/NOTIFY` bridge (`backend/conversation_bridge.py`).
