import { supabase } from '@/lib/supabase'

// US-040: thin client over the US-039 share endpoints. All three calls forward
// the caller's Supabase JWT so the backend's _assert_doc_owner runs as the
// real user and chunk_acl writes stay RLS-checked end-to-end.

const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(
  /\/$/,
  '',
)

export type PrincipalType = 'user' | 'group'

export type ShareSummary = {
  principal_type: PrincipalType
  principal_id: string
  display_name: string
  granted_at: string
}

export class ShareApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

async function authHeader(): Promise<HeadersInit> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new Error('Not signed in.')
  return { Authorization: `Bearer ${token}` }
}

export async function listShares(documentId: string): Promise<ShareSummary[]> {
  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/shares`, {
    headers: await authHeader(),
  })
  if (!res.ok) {
    throw new ShareApiError(res.status, await readError(res))
  }
  const payload = (await res.json()) as { shares: ShareSummary[] }
  return payload.shares ?? []
}

export async function grantShare(
  documentId: string,
  principalEmailOrName: string,
): Promise<ShareSummary> {
  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/share`, {
    method: 'POST',
    headers: { ...(await authHeader()), 'Content-Type': 'application/json' },
    body: JSON.stringify({ principal_email_or_name: principalEmailOrName }),
  })
  if (!res.ok) {
    throw new ShareApiError(res.status, await readError(res))
  }
  return (await res.json()) as ShareSummary
}

export async function revokeShare(
  documentId: string,
  principalType: PrincipalType,
  principalId: string,
): Promise<void> {
  const res = await fetch(
    `${BACKEND_URL}/api/documents/${documentId}/share/${principalType}/${principalId}`,
    {
      method: 'DELETE',
      headers: await authHeader(),
    },
  )
  if (!res.ok && res.status !== 204) {
    throw new ShareApiError(res.status, await readError(res))
  }
}

// US-086: share-to-bot is a SEPARATE, explicitly-confirmed "publish to the public
// support widget" action — never the normal grant box (the backend refuses the bot
// there and filters it from listShares). These three calls drive the distinct
// /publish-to-bot surface. `bot_provisioned=false` means support is not enabled for
// the workspace yet, so publishing is unavailable until an admin enables it (US-090).

export type BotPublishStatus = {
  published: boolean
  bot_provisioned: boolean
}

export async function getBotPublishStatus(documentId: string): Promise<BotPublishStatus> {
  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/publish-to-bot`, {
    headers: await authHeader(),
  })
  if (!res.ok) {
    throw new ShareApiError(res.status, await readError(res))
  }
  return (await res.json()) as BotPublishStatus
}

export async function publishToBot(documentId: string): Promise<BotPublishStatus> {
  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/publish-to-bot`, {
    method: 'POST',
    headers: await authHeader(),
  })
  if (!res.ok) {
    throw new ShareApiError(res.status, await readError(res))
  }
  return (await res.json()) as BotPublishStatus
}

export async function unpublishFromBot(documentId: string): Promise<void> {
  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/publish-to-bot`, {
    method: 'DELETE',
    headers: await authHeader(),
  })
  if (!res.ok && res.status !== 204) {
    throw new ShareApiError(res.status, await readError(res))
  }
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string; error?: string }
    return body.detail ?? body.error ?? `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}
