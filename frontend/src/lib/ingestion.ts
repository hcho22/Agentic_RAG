import { supabase } from '@/lib/supabase'

const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '')

export type DocumentStatus = 'queued' | 'processing' | 'ready' | 'error'

export type DocumentRow = {
  id: string
  user_id: string
  filename: string
  storage_path: string
  byte_size: number
  content_type: string | null
  status: DocumentStatus
  error_message: string | null
  chunks_count: number
  uploaded_at: string
  deleted_at: string | null
}

const DOCUMENT_COLUMNS =
  'id, user_id, filename, storage_path, byte_size, content_type, status, error_message, chunks_count, uploaded_at, deleted_at'

export const ACCEPTED_EXTENSIONS = ['.txt', '.md'] as const
export const ACCEPTED_MIME_TYPES = new Set([
  'text/plain',
  'text/markdown',
  'text/x-markdown',
  // some browsers send empty type for .md; we also validate by extension below.
])

export function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase()
  const extOk = ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))
  if (!extOk) return false
  // Trust the extension first (markdown often has empty/odd MIME); fall through.
  return true
}

export async function listDocuments(): Promise<DocumentRow[]> {
  const { data, error } = await supabase
    .from('documents')
    .select(DOCUMENT_COLUMNS)
    .is('deleted_at', null)
    .order('uploaded_at', { ascending: false })
  if (error) throw error
  return (data ?? []) as DocumentRow[]
}

// Uploads the raw file to Storage, creates a documents row, then triggers the
// backend chunking pipeline. The backend flips status=ready and fills in
// chunks_count once chunks are persisted (US-008).
export async function uploadDocument(userId: string, file: File): Promise<DocumentRow> {
  if (!isAcceptedFile(file)) {
    throw new Error(`Unsupported file type: ${file.name}. Only .txt and .md are accepted.`)
  }

  const insertRow = {
    user_id: userId,
    filename: file.name,
    storage_path: '', // filled in after we know the document id
    byte_size: file.size,
    content_type: file.type || null,
    status: 'processing' as DocumentStatus,
  }

  const { data: created, error: insertErr } = await supabase
    .from('documents')
    .insert(insertRow)
    .select(DOCUMENT_COLUMNS)
    .single()
  if (insertErr) throw insertErr
  const doc = created as DocumentRow

  const storagePath = `${userId}/${doc.id}/${file.name}`
  const { error: uploadErr } = await supabase.storage
    .from('documents')
    .upload(storagePath, file, {
      contentType: file.type || 'text/plain',
      upsert: false,
    })
  if (uploadErr) {
    // Roll back the row so the list stays clean — soft-delete via update since
    // RLS allows the owner to delete their own row.
    await supabase.from('documents').delete().eq('id', doc.id)
    throw uploadErr
  }

  const { data: pathed, error: pathErr } = await supabase
    .from('documents')
    .update({ storage_path: storagePath })
    .eq('id', doc.id)
    .select(DOCUMENT_COLUMNS)
    .single()
  if (pathErr) throw pathErr

  try {
    const updated = await triggerIngest(doc.id)
    return updated ?? (pathed as DocumentRow)
  } catch (e) {
    // Backend marks the row status=error on failure; just surface the error
    // to the caller so the UI can toast. Return the pathed row so the UI
    // still lists it (status will reflect 'error' on next realtime update).
    throw e
  }
}

async function triggerIngest(documentId: string): Promise<DocumentRow | null> {
  const { data: sess } = await supabase.auth.getSession()
  const token = sess.session?.access_token
  if (!token) throw new Error('Not signed in.')

  const res = await fetch(`${BACKEND_URL}/api/documents/${documentId}/ingest`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(text || `Ingestion failed (${res.status})`)
  }
  const payload = (await res.json()) as { document: DocumentRow | null }
  return payload.document ?? null
}

// Soft-delete: flags the row as deleted so it disappears from the UI list but
// the blob + row remain for audit / undo. Hard-delete + Storage cleanup is
// US-019 (cascade deletes on document removal).
export async function softDeleteDocument(documentId: string): Promise<void> {
  const { error } = await supabase
    .from('documents')
    .update({ deleted_at: new Date().toISOString() })
    .eq('id', documentId)
  if (error) throw error
}

export type DocumentRealtimeHandlers = {
  onInsert?: (row: DocumentRow) => void
  onUpdate?: (row: DocumentRow, old: Partial<DocumentRow>) => void
  onDelete?: (id: string) => void
}

// US-013: live-update the Ingestion list as the backend progresses a document
// through queued → processing → ready/error. Filters server-side on user_id
// to cut wire chatter — RLS still backstops cross-user isolation.
//
// Returns an unsubscribe function that tears down the channel; callers hand
// it straight back as a React effect cleanup.
export function subscribeToDocuments(
  userId: string,
  handlers: DocumentRealtimeHandlers,
): () => void {
  const channel = supabase
    .channel(`documents:${userId}`)
    .on(
      'postgres_changes',
      {
        event: '*',
        schema: 'public',
        table: 'documents',
        filter: `user_id=eq.${userId}`,
      },
      (payload) => {
        if (payload.eventType === 'INSERT') {
          handlers.onInsert?.(payload.new as DocumentRow)
        } else if (payload.eventType === 'UPDATE') {
          handlers.onUpdate?.(
            payload.new as DocumentRow,
            (payload.old ?? {}) as Partial<DocumentRow>,
          )
        } else if (payload.eventType === 'DELETE') {
          const id = (payload.old as { id?: string } | null)?.id
          if (id) handlers.onDelete?.(id)
        }
      },
    )
    .subscribe()

  return () => {
    void supabase.removeChannel(channel)
  }
}
