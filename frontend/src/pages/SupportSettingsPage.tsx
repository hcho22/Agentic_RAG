import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { AppHeader } from '@/components/AppHeader'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { resolveActiveWorkspace, type ActiveWorkspace } from '@/lib/supportQueue'
import {
  hasRegisteredOrigin,
  isWildcardOrigin,
  issueWidgetKey,
  listWidgetKeys,
  parseOrigins,
  revokeWidgetKey,
  rotateWidgetKey,
  type WidgetKey,
} from '@/lib/supportSettings'
import { listDocuments, type DocumentRow } from '@/lib/ingestion'
import { getBotPublishStatus, publishToBot, unpublishFromBot } from '@/lib/shares'

// US-090: the admin `/support/settings` route — the one place the administrative
// actions ADR-0002's `role='admin'` exists for live: enable support (provision
// the bot), manage widget keys (issue / rotate / revoke + per-key origins), and
// manage share-to-bot. Membership-gated surfaces (the /support/queue handoff
// list) are deliberately SEPARATE — this page is ROLE-gated (admin), that one is
// not. The UI gate here is cosmetic; the hard boundary is the widget_keys admin
// RLS enforced server-side under the caller's own JWT (US-072).
//
// SCOPE NOTE — per-key theming defaults: the shipped theming model is
// LOADER-DRIVEN (the buyer sets brand color / greeting / etc. as `data-*`
// attributes on the embed snippet, applied via the iframe `init` config, US-084);
// `widget_keys` has no theming columns and the resolve path returns none. So this
// page surfaces theming where it actually lives — on the copyable embed snippet —
// rather than storing per-key server-side defaults, which would be a separate
// cross-cutting change to the shipped public resolve/loader surfaces.

function relativeTime(iso: string | null): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return `${days}d ago`
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

// The loader snippet the buyer pastes on their page. `src` points at THIS kit's
// own origin (the loader derives the iframe origin from its own <script src>,
// US-083), so we build it from the current app origin. `data-public-key` is the
// only required attribute; theming is optional `data-*` (documented under the
// snippet).
function embedSnippet(publicKey: string): string {
  const origin = window.location.origin
  return `<script src="${origin}/widget.js"\n        data-public-key="${publicKey}"\n        async></script>`
}

export function SupportSettingsPage() {
  const { toast } = useToast()
  const [workspace, setWorkspace] = useState<ActiveWorkspace | null>(null)

  const resolved = workspace?.status === 'resolved' ? workspace : null
  const workspaceId = resolved?.workspaceId ?? null
  const isAdmin = resolved?.role === 'admin'

  // Widget keys are owned by the page so both the keys UI and the share-to-bot
  // section can read whether support is enabled (support === at least one key was
  // ever issued, since the first issuance lazily provisions the bot, US-069).
  const [keys, setKeys] = useState<WidgetKey[] | null>(null)

  useEffect(() => {
    let cancelled = false
    resolveActiveWorkspace()
      .then((w) => {
        if (!cancelled) setWorkspace(w)
      })
      .catch((e) => {
        if (cancelled) return
        toast(e instanceof Error ? e.message : 'Failed to resolve workspace', 'error')
        setWorkspace({ status: 'none' })
      })
    return () => {
      cancelled = true
    }
  }, [toast])

  const reloadKeys = useCallback(async () => {
    if (!workspaceId) return
    try {
      const rows = await listWidgetKeys(workspaceId)
      setKeys(rows)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load widget keys', 'error')
      setKeys([])
    }
  }, [workspaceId, toast])

  useEffect(() => {
    if (!isAdmin || !workspaceId) return
    void reloadKeys()
  }, [isAdmin, workspaceId, reloadKeys])

  const supportEnabled = (keys?.length ?? 0) > 0

  const statusNote = useMemo<ReactNode | null>(() => {
    if (!workspace) return <StatusNote>Loading…</StatusNote>
    if (workspace.status === 'none') {
      return (
        <StatusNote>
          You don’t belong to any workspace, so there are no support settings to manage.
        </StatusNote>
      )
    }
    if (workspace.status === 'ambiguous') {
      return (
        <StatusNote>
          You belong to multiple workspaces. Support settings apply to a single active
          workspace at a time, and there’s no workspace selector yet — cross-workspace
          management isn’t supported in v1.
        </StatusNote>
      )
    }
    if (!isAdmin) {
      return (
        <StatusNote>
          Support settings are available to workspace <span className="text-neutral-200">admins</span>{' '}
          only. The support queue is open to every member — you can reach it from the nav above.
        </StatusNote>
      )
    }
    return null
  }, [workspace, isAdmin])

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <AppHeader />
      <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col overflow-y-auto px-6 py-8">
        <div className="mb-6">
          <h2 className="text-xl font-semibold">Support settings</h2>
          <p className="mt-1 text-sm text-neutral-400">
            Enable your public support widget, manage its embed keys, and control which
            documents the bot can answer from. Admin-only.
          </p>
        </div>
        {statusNote ? (
          statusNote
        ) : workspaceId ? (
          <div className="space-y-10">
            <KeysSection
              workspaceId={workspaceId}
              keys={keys}
              supportEnabled={supportEnabled}
              onChanged={reloadKeys}
            />
            <ShareToBotSection
              workspaceId={workspaceId}
              supportEnabled={supportEnabled}
              keysLoaded={keys !== null}
            />
          </div>
        ) : null}
      </main>
    </div>
  )
}

function StatusNote({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-4 py-10 text-center text-sm text-neutral-400">
      {children}
    </div>
  )
}

function SectionHeader({ title, subtitle, action }: { title: string; subtitle: string; action?: ReactNode }) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div>
        <h3 className="text-base font-semibold text-neutral-100">{title}</h3>
        <p className="mt-1 text-sm text-neutral-400">{subtitle}</p>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Widget keys
// ---------------------------------------------------------------------------

type KeyForm = { mode: 'issue' } | { mode: 'rotate'; key: WidgetKey }

function KeysSection({
  workspaceId,
  keys,
  supportEnabled,
  onChanged,
}: {
  workspaceId: string
  keys: WidgetKey[] | null
  supportEnabled: boolean
  onChanged: () => Promise<void>
}) {
  const { toast } = useToast()
  const [form, setForm] = useState<KeyForm | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<WidgetKey | null>(null)
  const [revoking, setRevoking] = useState(false)

  const active = useMemo(() => (keys ?? []).filter((k) => !k.revoked_at), [keys])
  const revoked = useMemo(() => (keys ?? []).filter((k) => k.revoked_at), [keys])

  const handleRevoke = useCallback(async () => {
    if (!revokeTarget) return
    setRevoking(true)
    try {
      await revokeWidgetKey(revokeTarget.id)
      toast('Widget key revoked.')
      setRevokeTarget(null)
      await onChanged()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to revoke key', 'error')
    } finally {
      setRevoking(false)
    }
  }, [revokeTarget, toast, onChanged])

  return (
    <section>
      <SectionHeader
        title="Widget keys"
        subtitle="Issue a key to embed the widget on a site. Issuing your first key enables support (provisions your workspace bot). Rotate a key by issuing a replacement and revoking the old one."
        action={
          <Button size="sm" onClick={() => setForm({ mode: 'issue' })}>
            Issue key
          </Button>
        }
      />

      {!supportEnabled && keys !== null ? (
        <div className="mb-4 rounded-md border border-neutral-800 bg-neutral-900/40 px-4 py-3 text-sm text-neutral-300">
          Support isn’t enabled for this workspace yet. Issue your first widget key to
          provision the support bot and turn on the public widget.
        </div>
      ) : null}

      {keys === null ? (
        <p className="px-1 text-sm text-neutral-500">Loading keys…</p>
      ) : (
        <div className="space-y-3">
          {active.map((k) => (
            <KeyCard
              key={k.id}
              widgetKey={k}
              onRotate={() => setForm({ mode: 'rotate', key: k })}
              onRevoke={() => setRevokeTarget(k)}
            />
          ))}
          {active.length === 0 && supportEnabled ? (
            <p className="px-1 text-sm text-neutral-500">
              No active keys. Every key for this workspace has been revoked — issue a new one
              to keep the widget resolvable.
            </p>
          ) : null}
          {revoked.length > 0 ? (
            <details className="rounded-md border border-neutral-800 bg-neutral-900/30">
              <summary className="cursor-pointer select-none px-4 py-2 text-xs text-neutral-400">
                {revoked.length} revoked {revoked.length === 1 ? 'key' : 'keys'} (kept for audit)
              </summary>
              <div className="space-y-2 px-4 pb-3">
                {revoked.map((k) => (
                  <RevokedKeyRow key={k.id} widgetKey={k} />
                ))}
              </div>
            </details>
          ) : null}
        </div>
      )}

      {form ? (
        <KeyFormDialog
          workspaceId={workspaceId}
          form={form}
          onClose={() => setForm(null)}
          onDone={onChanged}
        />
      ) : null}

      <Dialog
        open={!!revokeTarget}
        onOpenChange={(o) => !revoking && !o && setRevokeTarget(null)}
        title="Revoke this widget key?"
      >
        <p className="text-sm text-neutral-300">
          Revoking blocks the key from starting <span className="font-medium">new</span>{' '}
          conversations. Any embed still using it will stop working, but live conversations
          already open are unaffected. This cannot be undone — issue a new key to replace it.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setRevokeTarget(null)} disabled={revoking}>
            Cancel
          </Button>
          <Button onClick={() => void handleRevoke()} disabled={revoking}>
            {revoking ? 'Revoking…' : 'Revoke key'}
          </Button>
        </div>
      </Dialog>
    </section>
  )
}

function KeyCard({
  widgetKey,
  onRotate,
  onRevoke,
}: {
  widgetKey: WidgetKey
  onRotate: () => void
  onRevoke: () => void
}) {
  const { toast } = useToast()
  const { public_key, label, allowed_origins, created_at } = widgetKey
  const hasOrigin = hasRegisteredOrigin(allowed_origins)
  const wildcard = allowed_origins.some((o) => isWildcardOrigin(o))

  const copy = useCallback(
    async (text: string, what: string) => {
      const ok = await copyToClipboard(text)
      toast(ok ? `${what} copied.` : `Couldn’t copy ${what.toLowerCase()}.`, ok ? 'default' : 'error')
    },
    [toast],
  )

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300">
              Active
            </span>
            <span className="truncate text-sm font-medium text-neutral-100">
              {label || 'Untitled key'}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-neutral-500">issued {relativeTime(created_at)}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={onRotate}>
            Rotate
          </Button>
          <Button variant="outline" size="sm" onClick={onRevoke}>
            Revoke
          </Button>
        </div>
      </div>

      <div className="mt-3">
        <Label className="text-xs text-neutral-500">Public key</Label>
        <div className="mt-1 flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-md border border-neutral-800 bg-neutral-950 px-2 py-1.5 font-mono text-xs text-neutral-300">
            {public_key}
          </code>
          <Button variant="ghost" size="sm" onClick={() => void copy(public_key, 'Public key')}>
            Copy
          </Button>
        </div>
      </div>

      <div className="mt-3">
        <Label className="text-xs text-neutral-500">Allowed origins</Label>
        {hasOrigin ? (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {allowed_origins.map((o) => (
              <span
                key={o}
                className={cn(
                  'rounded-md border px-2 py-0.5 font-mono text-xs',
                  isWildcardOrigin(o)
                    ? 'border-amber-900/60 bg-amber-950/30 text-amber-200'
                    : 'border-neutral-800 bg-neutral-950 text-neutral-300',
                )}
              >
                {o}
              </span>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-xs text-amber-300">
            No origins registered — this key is inactive and resolves nothing (fail-closed).
            Rotate it with at least one origin.
          </p>
        )}
        {wildcard ? (
          <p className="mt-1.5 text-xs text-amber-300">
            <span className="font-medium">“*” is a dev-only wildcard</span> — it admits any origin
            and must not be used in production. Register specific origins instead.
          </p>
        ) : null}
      </div>

      <div className="mt-3">
        <Label className="text-xs text-neutral-500">Embed snippet</Label>
        <div className="mt-1 flex items-start gap-2">
          <pre className="min-w-0 flex-1 overflow-x-auto rounded-md border border-neutral-800 bg-neutral-950 px-3 py-2 text-xs text-neutral-300">
            <code>{embedSnippet(public_key)}</code>
          </pre>
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0"
            onClick={() => void copy(embedSnippet(public_key), 'Embed snippet')}
          >
            Copy
          </Button>
        </div>
        <p className="mt-1.5 text-xs text-neutral-500">
          Theming is set on the snippet with optional attributes:{' '}
          <code className="text-neutral-400">data-brand-color</code>,{' '}
          <code className="text-neutral-400">data-greeting</code>,{' '}
          <code className="text-neutral-400">data-title</code>,{' '}
          <code className="text-neutral-400">data-position</code>,{' '}
          <code className="text-neutral-400">data-launcher-icon</code>. See the embed guide.
        </p>
      </div>
    </div>
  )
}

function RevokedKeyRow({ widgetKey }: { widgetKey: WidgetKey }) {
  const { public_key, label, revoked_at } = widgetKey
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-neutral-800 bg-neutral-950/40 px-3 py-2 opacity-70">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-[10px] font-medium text-neutral-400">
            Revoked
          </span>
          <span className="truncate text-xs text-neutral-400">{label || 'Untitled key'}</span>
        </div>
        <code className="mt-0.5 block truncate font-mono text-[11px] text-neutral-600">
          {public_key}
        </code>
      </div>
      <span className="shrink-0 text-[11px] text-neutral-600">revoked {relativeTime(revoked_at)}</span>
    </div>
  )
}

// Issue OR rotate. Rotation reuses this same form pre-filled from the old key,
// then (on submit) issues the replacement and revokes the old one. Mounted fresh
// each open (the parent conditionally renders it), so the initial-value useState
// captures the right defaults per mode.
function KeyFormDialog({
  workspaceId,
  form,
  onClose,
  onDone,
}: {
  workspaceId: string
  form: KeyForm
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const { toast } = useToast()
  const isRotate = form.mode === 'rotate'
  const initial = isRotate ? form.key : null
  const [label, setLabel] = useState(initial?.label ?? '')
  const [originsText, setOriginsText] = useState((initial?.allowed_origins ?? []).join('\n'))
  const [busy, setBusy] = useState(false)

  const origins = parseOrigins(originsText)
  const valid = hasRegisteredOrigin(origins)
  const wildcard = origins.some((o) => isWildcardOrigin(o))

  const submit = useCallback(async () => {
    if (!valid) return
    setBusy(true)
    try {
      const trimmedLabel = label.trim() || null
      if (form.mode === 'rotate') {
        const { revokedOld } = await rotateWidgetKey(form.key.id, {
          workspaceId,
          label: trimmedLabel,
          allowedOrigins: origins,
        })
        if (revokedOld) {
          toast('Key rotated — new key issued, old key revoked.')
        } else {
          // The replacement was issued but the old key could not be revoked; it
          // is still live. Tell the admin to revoke it manually rather than lose
          // the new key.
          toast(
            'New key issued, but the old key could NOT be revoked — revoke it manually from the list.',
            'error',
          )
        }
      } else {
        await issueWidgetKey({ workspaceId, label: trimmedLabel, allowedOrigins: origins })
        toast('Widget key issued.')
      }
      onClose()
      await onDone()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to issue key', 'error')
    } finally {
      setBusy(false)
    }
  }, [valid, label, form, workspaceId, origins, toast, onClose, onDone])

  return (
    <Dialog
      open
      onOpenChange={(o) => !busy && !o && onClose()}
      title={isRotate ? 'Rotate widget key' : 'Issue widget key'}
      description={
        isRotate
          ? 'Issues a replacement key and revokes the current one. Update the embed on your site with the new key.'
          : 'The first key you issue enables support and provisions your workspace bot.'
      }
    >
      <div className="space-y-4">
        <div>
          <Label htmlFor="key-label">Label</Label>
          <Input
            id="key-label"
            className="mt-1"
            placeholder="e.g. Marketing site"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            disabled={busy}
          />
          <p className="mt-1 text-xs text-neutral-500">A name to recognize this key. Optional.</p>
        </div>
        <div>
          <Label htmlFor="key-origins">Allowed origins</Label>
          <Textarea
            id="key-origins"
            className="mt-1 h-24 font-mono text-xs"
            placeholder={'https://www.example.com\nhttps://app.example.com'}
            value={originsText}
            onChange={(e) => setOriginsText(e.target.value)}
            disabled={busy}
          />
          <p className="mt-1 text-xs text-neutral-500">
            One origin per line (scheme + host, e.g.{' '}
            <code className="text-neutral-400">https://www.example.com</code>). Matched exactly —
            no path, no trailing slash. A key with no origin never resolves (fail-closed).
          </p>
          {!valid ? (
            <p className="mt-1 text-xs text-amber-300">
              Register at least one origin — a key with none is inactive.
            </p>
          ) : null}
          {wildcard ? (
            <p className="mt-1 text-xs text-amber-300">
              <span className="font-medium">“*” is a dev-only wildcard</span> — never use it in
              production.
            </p>
          ) : null}
        </div>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => void submit()} disabled={busy || !valid}>
          {busy ? 'Working…' : isRotate ? 'Rotate key' : 'Issue key'}
        </Button>
      </div>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// Share-to-bot
// ---------------------------------------------------------------------------

type PublishState = { published: boolean; loading: boolean }

// Manages share-to-bot (US-086) for the documents the admin OWNS. `listDocuments`
// reads under the caller's owner-only RLS, and publish/unpublish are owner-gated
// on the backend — so this surface is exactly the admin's own documents, which is
// the correct scope (publishing a doc to the public widget is the owner's call).
// Publishing is a loud, explicitly-confirmed action: it makes the document
// answerable to ANYONE who can reach the public widget.
function ShareToBotSection({
  workspaceId,
  supportEnabled,
  keysLoaded,
}: {
  workspaceId: string
  supportEnabled: boolean
  keysLoaded: boolean
}) {
  const { toast } = useToast()
  const [docs, setDocs] = useState<DocumentRow[] | null>(null)
  const [publishState, setPublishState] = useState<Record<string, PublishState>>({})
  const [confirmDoc, setConfirmDoc] = useState<DocumentRow | null>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)

  // Load the admin's own ready documents, then their per-doc publish status.
  // Only fetch publish status when support is enabled — with no bot provisioned
  // every status is trivially (false, not-provisioned) and the actions are off.
  useEffect(() => {
    let cancelled = false
    setDocs(null)
    setPublishState({})
    listDocuments()
      .then(async (all) => {
        if (cancelled) return
        const ready = all.filter((d) => d.status === 'ready')
        setDocs(ready)
        if (!supportEnabled || ready.length === 0) return
        const entries = await Promise.all(
          ready.map(async (d) => {
            try {
              const s = await getBotPublishStatus(d.id)
              return [d.id, { published: s.published, loading: false }] as const
            } catch {
              return [d.id, { published: false, loading: false }] as const
            }
          }),
        )
        if (!cancelled) setPublishState(Object.fromEntries(entries))
      })
      .catch((e) => {
        if (cancelled) return
        toast(e instanceof Error ? e.message : 'Failed to load documents', 'error')
        setDocs([])
      })
    return () => {
      cancelled = true
    }
    // workspaceId is stable per active workspace; re-run if support flips on.
  }, [workspaceId, supportEnabled, toast])

  const setDocPublished = useCallback((id: string, published: boolean) => {
    setPublishState((prev) => ({ ...prev, [id]: { published, loading: false } }))
  }, [])

  const handleUnpublish = useCallback(
    async (doc: DocumentRow) => {
      setPublishState((prev) => ({ ...prev, [doc.id]: { published: true, loading: true } }))
      try {
        await unpublishFromBot(doc.id)
        setDocPublished(doc.id, false)
        toast('Unpublished from the widget.')
      } catch (e) {
        setDocPublished(doc.id, true)
        toast(e instanceof Error ? e.message : 'Failed to unpublish', 'error')
      }
    },
    [toast, setDocPublished],
  )

  const handleConfirmPublish = useCallback(async () => {
    if (!confirmDoc) return
    setConfirmBusy(true)
    try {
      await publishToBot(confirmDoc.id)
      setDocPublished(confirmDoc.id, true)
      toast('Published to the widget.')
      setConfirmDoc(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to publish', 'error')
    } finally {
      setConfirmBusy(false)
    }
  }, [confirmDoc, toast, setDocPublished])

  return (
    <section>
      <SectionHeader
        title="Share to bot"
        subtitle="Publish a document to make its contents answerable by the public support widget. Only documents you own are listed here."
      />

      {!keysLoaded ? (
        <p className="px-1 text-sm text-neutral-500">Loading…</p>
      ) : !supportEnabled ? (
        <div className="rounded-md border border-neutral-800 bg-neutral-900/40 px-4 py-3 text-sm text-neutral-400">
          Enable support first (issue a widget key above) before publishing documents to the bot.
        </div>
      ) : docs === null ? (
        <p className="px-1 text-sm text-neutral-500">Loading documents…</p>
      ) : docs.length === 0 ? (
        <p className="px-1 text-sm text-neutral-500">
          You don’t own any ready documents yet. Upload and ingest a document, then publish it here.
        </p>
      ) : (
        <div className="space-y-2">
          {docs.map((d) => (
            <DocPublishRow
              key={d.id}
              doc={d}
              state={publishState[d.id]}
              onPublish={() => setConfirmDoc(d)}
              onUnpublish={() => void handleUnpublish(d)}
            />
          ))}
        </div>
      )}

      <Dialog
        open={!!confirmDoc}
        onOpenChange={(o) => !confirmBusy && !o && setConfirmDoc(null)}
        title="Publish to the public support widget?"
      >
        <p className="text-sm text-neutral-300">
          The contents of{' '}
          <span className="font-medium">“{confirmDoc?.metadata?.title || confirmDoc?.filename}”</span>{' '}
          will become{' '}
          <span className="font-medium text-amber-300">
            answerable to anyone who can reach your public support widget
          </span>
          . The support bot can synthesize answers from this document for any visitor — this is not
          the same as sharing it with a teammate.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmDoc(null)} disabled={confirmBusy}>
            Cancel
          </Button>
          <Button onClick={() => void handleConfirmPublish()} disabled={confirmBusy}>
            {confirmBusy ? 'Publishing…' : 'Publish to widget'}
          </Button>
        </div>
      </Dialog>
    </section>
  )
}

function DocPublishRow({
  doc,
  state,
  onPublish,
  onUnpublish,
}: {
  doc: DocumentRow
  state: PublishState | undefined
  onPublish: () => void
  onUnpublish: () => void
}) {
  const title = doc.metadata?.title || doc.filename
  const published = state?.published ?? false
  const loading = state?.loading ?? state === undefined
  return (
    <div
      className={cn(
        'flex items-center justify-between gap-4 rounded-md border px-3 py-2.5',
        published ? 'border-amber-900/60 bg-amber-950/20' : 'border-neutral-800 bg-neutral-900/40',
      )}
    >
      <div className="min-w-0">
        <p className="truncate text-sm text-neutral-200">{title}</p>
        {published ? (
          <p className="text-xs text-amber-200/90">
            Published — answerable via the public widget.
          </p>
        ) : (
          <p className="truncate text-xs text-neutral-500">{doc.filename}</p>
        )}
      </div>
      {loading ? (
        <span className="shrink-0 text-xs text-neutral-500">…</span>
      ) : published ? (
        <Button variant="outline" size="sm" className="shrink-0" onClick={onUnpublish}>
          Unpublish
        </Button>
      ) : (
        <Button variant="outline" size="sm" className="shrink-0" onClick={onPublish}>
          Publish…
        </Button>
      )}
    </div>
  )
}
