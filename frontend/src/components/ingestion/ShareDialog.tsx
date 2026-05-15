import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useToast } from '@/components/ui/toast'
import { ShareApiError, grantShare, listShares, revokeShare, type ShareSummary } from '@/lib/shares'

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

  useEffect(() => {
    if (!open) return setInput('')
    let cancelled = false
    setLoading(true)
    listShares(documentId)
      .then((rows) => !cancelled && setShares(rows))
      .catch((e: unknown) =>
        !cancelled && toast(e instanceof Error ? e.message : 'Failed to load shares', 'error'),
      )
      .finally(() => !cancelled && setLoading(false))
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={`Share "${filename}"`}>
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
    </Dialog>
  )
}
