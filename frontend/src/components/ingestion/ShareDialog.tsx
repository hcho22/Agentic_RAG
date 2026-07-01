import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useToast } from '@/components/ui/toast'
import {
  ShareApiError,
  getBotPublishStatus,
  grantShare,
  listShares,
  publishToBot,
  revokeShare,
  unpublishFromBot,
  type BotPublishStatus,
  type ShareSummary,
} from '@/lib/shares'

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  documentId: string
  filename: string
  ownerEmail: string
}

const keyOf = (s: Pick<ShareSummary, 'principal_type' | 'principal_id'>) =>
  `${s.principal_type}:${s.principal_id}`

export function ShareDialog({ open, onOpenChange, documentId, filename, ownerEmail }: Props) {
  const { toast } = useToast()
  const [shares, setShares] = useState<ShareSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [input, setInput] = useState('')
  // US-086: share-to-bot is a SEPARATE, explicitly-confirmed "publish to the
  // public support widget" action — never the normal grant box above. The bot
  // never appears as a grantee row (the backend filters it from listShares and
  // refuses it in the grant box), so its state lives in its own section here.
  const [publishStatus, setPublishStatus] = useState<BotPublishStatus | null>(null)
  const [publishState, setPublishState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [publishBusy, setPublishBusy] = useState(false)

  useEffect(() => {
    if (!open) {
      setInput('')
      setConfirmOpen(false)
      return
    }
    let cancelled = false
    setLoading(true)
    listShares(documentId)
      .then((rows) => !cancelled && setShares(rows))
      .catch((e: unknown) =>
        !cancelled && toast(e instanceof Error ? e.message : 'Failed to load shares', 'error'),
      )
      .finally(() => !cancelled && setLoading(false))
    // Publish status loads independently so a hiccup here never breaks the share list.
    setPublishState('loading')
    setPublishStatus(null)
    getBotPublishStatus(documentId)
      .then((status) => {
        if (cancelled) return
        setPublishStatus(status)
        setPublishState('ready')
      })
      .catch(() => !cancelled && setPublishState('error'))
    return () => {
      cancelled = true
    }
  }, [open, documentId, toast])

  async function handleGrant() {
    const value = input.trim()
    if (!value) return
    setBusy('grant')
    try {
      await grantShare(documentId, value)
      setShares(await listShares(documentId))
      setInput('')
      toast(`Granted access to ${value}`)
    } catch (e: unknown) {
      // A 403 here is the US-086 bot refusal — the backend's detail already tells
      // the owner to use "Publish to public support widget", so surface it verbatim.
      const msg =
        e instanceof ShareApiError && e.status === 404
          ? 'No user or group with that identifier. They have to sign up first.'
          : e instanceof Error ? e.message : 'Failed to grant access'
      toast(msg, 'error')
    } finally {
      setBusy(null)
    }
  }

  async function handleRevoke(s: ShareSummary) {
    setBusy(keyOf(s))
    try {
      await revokeShare(documentId, s.principal_type, s.principal_id)
      setShares((prev) => prev.filter((p) => keyOf(p) !== keyOf(s)))
      toast(`Revoked access from ${s.display_name}`)
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to revoke access', 'error')
    } finally {
      setBusy(null)
    }
  }

  async function handleConfirmPublish() {
    setPublishBusy(true)
    try {
      const status = await publishToBot(documentId)
      setPublishStatus(status)
      setPublishState('ready')
      setConfirmOpen(false)
      toast('Published to the public support widget')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to publish', 'error')
    } finally {
      setPublishBusy(false)
    }
  }

  async function handleUnpublish() {
    setPublishBusy(true)
    try {
      await unpublishFromBot(documentId)
      setPublishStatus((prev) => (prev ? { ...prev, published: false } : prev))
      toast('Unpublished from the support widget')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to unpublish', 'error')
    } finally {
      setPublishBusy(false)
    }
  }

  return (
    <>
      <Dialog
        open={open}
        // Keep the share dialog open while the publish confirmation is up, so a
        // stray Escape/backdrop dismisses only the confirmation, not both.
        onOpenChange={(o) => {
          if (!o && confirmOpen) return
          onOpenChange(o)
        }}
        title={`Share "${filename}"`}
      >
        <div className="flex gap-2">
          <Input
            placeholder="someone@example.com or group-name"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void handleGrant()}
            disabled={busy === 'grant'}
          />
          <Button onClick={() => void handleGrant()} disabled={busy === 'grant' || !input.trim()}>
            {busy === 'grant' ? 'Granting…' : 'Grant'}
          </Button>
        </div>
        <ul className="mt-4 space-y-1 text-sm">
          <li className="flex items-center justify-between rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2">
            <span className="text-neutral-200">
              <span className="font-medium">You (owner)</span>
              <span className="ml-1 text-neutral-500">— {ownerEmail} — full access</span>
            </span>
          </li>
          {loading && <li className="px-3 py-2 text-xs text-neutral-500">Loading shares…</li>}
          {!loading && shares.map((s) => (
            <li key={keyOf(s)} className="flex items-center justify-between rounded-md border border-neutral-800 px-3 py-2">
              <span className="text-neutral-200">
                {s.display_name}
                <span className="ml-1 text-xs text-neutral-500">({s.principal_type})</span>
              </span>
              <button
                className="rounded-md px-2 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100 disabled:opacity-50"
                onClick={() => void handleRevoke(s)}
                disabled={busy === keyOf(s)}
                aria-label={`Revoke access for ${s.display_name}`}
              >
                {busy === keyOf(s) ? '…' : '×'}
              </button>
            </li>
          ))}
        </ul>

        {/* US-086: publish-to-bot — a distinct, loud, explicitly-confirmed action,
            visually separated from the teammate grant box above. */}
        <div className="mt-5 border-t border-neutral-800 pt-4">
          <h4 className="text-sm font-medium text-neutral-200">Public support widget</h4>
          {publishState === 'loading' && (
            <p className="mt-1 text-xs text-neutral-500">Checking publish status…</p>
          )}
          {publishState === 'error' && (
            <p className="mt-1 text-xs text-neutral-500">Publish status unavailable.</p>
          )}
          {publishState === 'ready' && publishStatus && !publishStatus.bot_provisioned && (
            <p className="mt-1 text-xs text-neutral-500">
              Support isn’t enabled for this workspace yet. An admin can enable it in Support
              settings before documents can be published to the widget.
            </p>
          )}
          {publishState === 'ready' && publishStatus?.bot_provisioned && publishStatus.published && (
            <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-amber-900/60 bg-amber-950/30 px-3 py-2">
              <span className="text-xs text-amber-200">
                Published — answerable to anyone who can reach your public support widget.
              </span>
              <Button
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={() => void handleUnpublish()}
                disabled={publishBusy}
              >
                {publishBusy ? '…' : 'Unpublish'}
              </Button>
            </div>
          )}
          {publishState === 'ready' && publishStatus?.bot_provisioned && !publishStatus.published && (
            <div className="mt-2 flex items-start justify-between gap-3">
              <p className="text-xs text-neutral-400">
                Publishing lets the support bot answer{' '}
                <span className="font-medium text-neutral-200">anyone who can reach your public widget</span>{' '}
                from this document’s contents. This is separate from sharing with a teammate.
              </p>
              <Button
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={() => setConfirmOpen(true)}
                disabled={publishBusy}
              >
                Publish…
              </Button>
            </div>
          )}
        </div>
      </Dialog>

      {/* The explicit confirmation — states the consequence before anything is published. */}
      <Dialog
        open={confirmOpen}
        onOpenChange={(o) => !publishBusy && setConfirmOpen(o)}
        title="Publish to the public support widget?"
      >
        <p className="text-sm text-neutral-300">
          The contents of <span className="font-medium">“{filename}”</span> will become{' '}
          <span className="font-medium text-amber-300">
            answerable to anyone who can reach your public support widget
          </span>
          . The support bot can synthesize answers from this document for any visitor — this is not
          the same as sharing it with a teammate.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={publishBusy}>
            Cancel
          </Button>
          <Button onClick={() => void handleConfirmPublish()} disabled={publishBusy}>
            {publishBusy ? 'Publishing…' : 'Publish to widget'}
          </Button>
        </div>
      </Dialog>
    </>
  )
}
