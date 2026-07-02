import { supabase } from '@/lib/supabase'

// US-090: the admin `/support/settings` client. Widget-key management (issue /
// rotate / revoke) is done through the US-072 backend endpoints, which run the
// INSERT/SELECT/UPDATE UNDER THE ADMIN'S OWN Supabase JWT — so the
// `widget_keys_*_admin` RLS (the ADR-0002 membership clause WITH `role='admin'`,
// the one place `role` legitimately enters a predicate) IS the authorization. A
// non-admin's issue is rejected by Postgres (403), their list reads back empty,
// and their revoke matches zero rows (404). The page front-gates the UI on the
// admin role for UX, but the security boundary is server-side RLS, never this
// client. There is deliberately NO rotate endpoint: rotation is issue-new +
// revoke-old, composed here (see `rotateWidgetKey`).

const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(
  /\/$/,
  '',
)

// The dev-only wildcard opt-in (US-073 `WILDCARD_ORIGIN`). A key whose
// allowed_origins contains "*" matches any PRESENT Origin — non-production only.
export const WILDCARD_ORIGIN = '*'

// The public key returned at issuance is NON-SECRET (US-072): it is embedded
// verbatim in the buyer's page JS and only NAMES which workspace's bot a widget
// speaks to. Safe to display, copy, and put in the loader snippet. `revoked_at`
// null == active; a revoked key blocks NEW conversations but never terminates a
// live one (the per-conversation opaque token is independent once minted).
export type WidgetKey = {
  id: string
  workspace_id: string
  public_key: string
  label: string | null
  allowed_origins: string[]
  revoked_at: string | null
  created_at: string
  // Present on issue/revoke (PostgREST return=representation); the list select
  // omits it. Not used by the UI, kept optional for shape fidelity.
  created_by?: string | null
}

export class SupportSettingsApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

async function authHeader(): Promise<HeadersInit> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new Error('Not signed in.')
  return { Authorization: `Bearer ${token}` }
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string; error?: string }
    return body.detail ?? body.error ?? `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

// Lists every key for the workspace — ACTIVE and REVOKED, newest first — so the
// settings UI can show a rotated key's old (revoked) entry beside its live
// replacement. RLS scopes rows to workspaces the caller admins; a non-admin
// reads back `[]` (no leak), which the page treats the same as "no keys yet".
export async function listWidgetKeys(workspaceId: string): Promise<WidgetKey[]> {
  const res = await fetch(
    `${BACKEND_URL}/api/support/widget-keys?workspace_id=${encodeURIComponent(workspaceId)}`,
    { headers: await authHeader() },
  )
  if (!res.ok) {
    throw new SupportSettingsApiError(res.status, await readError(res))
  }
  const body = (await res.json()) as { widget_keys: WidgetKey[] }
  return body.widget_keys ?? []
}

// Issues a new key (label + registered origins). The FIRST issuance for a
// workspace lazily provisions the support bot (US-069) — i.e. issuing a key is
// what ENABLES support; there is no separate enable toggle. The backend rejects
// an empty/blank-only origin list with a 400 (a key with no origin never
// resolves, US-073 fail-closed), which `hasRegisteredOrigin` mirrors client-side
// so we never round-trip a dead-on-arrival key.
export async function issueWidgetKey(params: {
  workspaceId: string
  label: string | null
  allowedOrigins: string[]
}): Promise<WidgetKey> {
  const res = await fetch(`${BACKEND_URL}/api/support/widget-keys`, {
    method: 'POST',
    headers: { ...(await authHeader()), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspace_id: params.workspaceId,
      label: params.label,
      allowed_origins: params.allowedOrigins,
    }),
  })
  if (!res.ok) {
    throw new SupportSettingsApiError(res.status, await readError(res))
  }
  const body = (await res.json()) as { widget_key: WidgetKey }
  return body.widget_key
}

// Revokes a key (one-way flip of `revoked_at`, DB-enforced latch). Idempotent on
// the backend: a key with no active row to flip returns 404, which we surface.
export async function revokeWidgetKey(keyId: string): Promise<WidgetKey> {
  const res = await fetch(
    `${BACKEND_URL}/api/support/widget-keys/${encodeURIComponent(keyId)}/revoke`,
    { method: 'POST', headers: await authHeader() },
  )
  if (!res.ok) {
    throw new SupportSettingsApiError(res.status, await readError(res))
  }
  const body = (await res.json()) as { widget_key: WidgetKey }
  return body.widget_key
}

export type RotateResult = {
  // The freshly-issued replacement key (its public_key is the one the buyer must
  // swap into their loader snippet).
  issued: WidgetKey
  // The old key, now revoked. Null only if the revoke leg failed after a
  // successful issue (see below) — the caller must warn and offer a manual
  // revoke, because the old key is still live.
  revokedOld: WidgetKey | null
}

// Rotation = issue-new THEN revoke-old (there is no atomic rotate endpoint, by
// design — the migration allows multiple keys per workspace precisely so an old
// key lingers, revoked, beside its replacement for audit). The new key can carry
// edited label/origins. Ordering matters: we issue FIRST so a failure leaves the
// old key untouched (fail-safe — the buyer's embedded loader keeps working);
// only once the replacement exists do we revoke the old one. If the revoke leg
// fails, we still return the issued key with `revokedOld: null` so the caller can
// tell the admin the old key is live and must be revoked manually — we never
// silently drop the new key.
export async function rotateWidgetKey(
  oldKeyId: string,
  next: { label: string | null; allowedOrigins: string[]; workspaceId: string },
): Promise<RotateResult> {
  const issued = await issueWidgetKey({
    workspaceId: next.workspaceId,
    label: next.label,
    allowedOrigins: next.allowedOrigins,
  })
  try {
    const revokedOld = await revokeWidgetKey(oldKeyId)
    return { issued, revokedOld }
  } catch {
    // The new key is live; the old one could not be revoked. Report partial
    // success so the UI can prompt a manual revoke rather than lose the new key.
    return { issued, revokedOld: null }
  }
}

// WRITE-side mirror of the US-073 resolution gate: a key is potentially-functional
// iff at least one origin is non-blank (the dev-only "*" counts). Used to disable
// the issue/rotate submit and show the fail-closed reminder before we ever call
// the backend (which 400s the same case). Asserts only that the key is not
// dead-on-arrival — origins are otherwise compared exactly/un-normalized at
// resolution, so a mis-typed origin still fails CLOSED there.
export function hasRegisteredOrigin(origins: string[]): boolean {
  return origins.some((o) => o.trim().length > 0)
}

export function isWildcardOrigin(origin: string): boolean {
  return origin.trim() === WILDCARD_ORIGIN
}

// Parse a textarea (one origin per line, commas also accepted) into a trimmed,
// de-duplicated, blank-free origin list — the canonical scheme+host[+port] form
// the browser emits in `Origin` (US-073 compares exactly, so we do NOT normalize
// case/slashes; the admin must register origins in that exact form).
export function parseOrigins(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of text.split(/[\n,]/)) {
    const origin = raw.trim()
    if (!origin || seen.has(origin)) continue
    seen.add(origin)
    out.push(origin)
  }
  return out
}
