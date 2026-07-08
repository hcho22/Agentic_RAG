import { supabase } from '@/lib/supabase'

// US-001/US-007 (ADR-0002): a Workspace is the hard tenant boundary. `role` is
// administrative only and grants no content access.
export type WorkspaceRole = 'admin' | 'member'

// US-007 active-workspace resolution, applied CLIENT-SIDE and shared by every
// surface that has to act "as a workspace" — the support queue/settings AND
// document ingestion: default-when-sole, error-on-ambiguous, explicit "none".
// Reads the caller's own `workspace_membership` rows (RLS:
// workspace_membership_select_own scopes the result to auth.uid()), so this
// never crosses the tenant boundary. There is no cross-workspace view in v1 — a
// single active workspace per action — so an ambiguous (≥2) membership set is
// surfaced as a resolvable state rather than silently guessing a workspace
// (mirrors the backend's 400-on-ambiguous posture).
export type ActiveWorkspace =
  | { status: 'resolved'; workspaceId: string; role: WorkspaceRole }
  | { status: 'none' }
  | { status: 'ambiguous'; workspaceIds: string[] }

export async function resolveActiveWorkspace(): Promise<ActiveWorkspace> {
  const { data, error } = await supabase
    .from('workspace_membership')
    .select('workspace_id, role')
    .order('created_at', { ascending: true })
  if (error) throw error

  const rows = (data ?? []) as { workspace_id: string; role: WorkspaceRole }[]
  if (rows.length === 0) return { status: 'none' }
  if (rows.length === 1) {
    return { status: 'resolved', workspaceId: rows[0].workspace_id, role: rows[0].role }
  }
  return { status: 'ambiguous', workspaceIds: rows.map((r) => r.workspace_id) }
}
