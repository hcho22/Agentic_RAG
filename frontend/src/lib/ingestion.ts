import { supabase } from '@/lib/supabase'

const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '')

export type DocumentStatus = 'queued' | 'processing' | 'ready' | 'error'

// US-016: LLM-extracted structured metadata. Shape pinned by the backend
// Pydantic `DocumentMetadata` schema — kept as a loose type on the TS side
// because the JSONB column is null until ingestion populates it and
// extraction is allowed to fail non-fatally.
export type DocumentMetadata = {
  title: string
  authors: string[]
  topics: string[]
  published_date: string | null
  document_type: string
}

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
  content_hash: string | null
  metadata: DocumentMetadata | null
}

const DOCUMENT_COLUMNS =
  'id, user_id, filename, storage_path, byte_size, content_type, status, error_message, chunks_count, uploaded_at, deleted_at, content_hash, metadata'

// US-014: SHA-256 of the raw file bytes, lower-case hex. Matches hashlib.sha256
// on the backend so the two sides can agree on a single content-addressable id.
async function sha256Hex(file: File): Promise<string> {
  const buf = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

export type UploadResult = {
  document: DocumentRow
  // True when the file was already ingested under this user — no new row or
  // blob was created; `document` is the existing one.
  duplicate: boolean
}

// US-018: multi-format support. Docling on the backend parses PDF / DOCX /
// HTML / MD; .txt flows through as a straight utf-8 decode. Extension is the
// source of truth for acceptance — browsers are unreliable about `file.type`
// for Markdown and sometimes for HTML from local disk.
export const ACCEPTED_EXTENSIONS = ['.txt', '.md', '.pdf', '.docx', '.html'] as const
export const ACCEPTED_MIME_TYPES = new Set([
  'text/plain',
  'text/markdown',
  'text/x-markdown',
  'text/html',
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
])

export function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))
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
//
// US-014: dedupe by SHA-256 of file bytes. If the user already has a live
// document with the same hash, return that row instead of re-uploading.
export async function uploadDocument(userId: string, file: File): Promise<UploadResult> {
  if (!isAcceptedFile(file)) {
    throw new Error(
      `Unsupported file type: ${file.name}. Accepted: ${ACCEPTED_EXTENSIONS.join(', ')}.`,
    )
  }

  const contentHash = await sha256Hex(file)

  const { data: existing, error: lookupErr } = await supabase
    .from('documents')
    .select(DOCUMENT_COLUMNS)
    .eq('user_id', userId)
    .eq('content_hash', contentHash)
    .is('deleted_at', null)
    .maybeSingle()
  if (lookupErr) throw lookupErr
  if (existing) {
    return { document: existing as DocumentRow, duplicate: true }
  }

  const insertRow = {
    user_id: userId,
    filename: file.name,
    storage_path: '', // filled in after we know the document id
    byte_size: file.size,
    content_type: file.type || null,
    status: 'processing' as DocumentStatus,
    content_hash: contentHash,
  }

  const { data: created, error: insertErr } = await supabase
    .from('documents')
    .insert(insertRow)
    .select(DOCUMENT_COLUMNS)
    .single()
  if (insertErr) {
    // Race: another concurrent upload of the same file won the unique index.
    // Fetch and return the winner rather than erroring out.
    if (insertErr.code === '23505') {
      const { data: raced } = await supabase
        .from('documents')
        .select(DOCUMENT_COLUMNS)
        .eq('user_id', userId)
        .eq('content_hash', contentHash)
        .is('deleted_at', null)
        .maybeSingle()
      if (raced) return { document: raced as DocumentRow, duplicate: true }
    }
    throw insertErr
  }
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

  const updated = await triggerIngest(doc.id)
  return { document: updated ?? (pathed as DocumentRow), duplicate: false }
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

// US-019: hard delete. The DB row is removed first so chunks + embeddings
// cascade via FK (chunks.document_id references documents(id) ON DELETE
// CASCADE). The Storage blob is removed as a compensating action after the
// row is gone — if this fails we log and continue, leaving an orphan object
// rather than an orphan row. Order matters: an orphan row is user-visible
// (broken document in the list); an orphan blob is not (user_id-scoped,
// cleanable out-of-band). The UI reflects the deletion via the Realtime
// DELETE event (documents has REPLICA IDENTITY FULL so the pre-image carries
// the id).
export async function deleteDocument(
  doc: Pick<DocumentRow, 'id' | 'storage_path'>,
): Promise<void> {
  const { error: rowErr } = await supabase.from('documents').delete().eq('id', doc.id)
  if (rowErr) throw rowErr

  if (doc.storage_path) {
    const { error: blobErr } = await supabase.storage
      .from('documents')
      .remove([doc.storage_path])
    if (blobErr) {
      console.warn(
        `deleteDocument: row ${doc.id} removed but blob ${doc.storage_path} still present: ${blobErr.message}`,
      )
    }
  }
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
