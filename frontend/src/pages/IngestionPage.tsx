import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '@/contexts/AuthContext'
import { useToast } from '@/components/ui/toast'
import { AppHeader } from '@/components/AppHeader'
import { DropZone } from '@/components/ingestion/DropZone'
import { DocumentsTable } from '@/components/ingestion/DocumentsTable'
import {
  isAcceptedFile,
  listDocuments,
  softDeleteDocument,
  subscribeToDocuments,
  uploadDocument,
  type DocumentRow,
} from '@/lib/ingestion'

export function IngestionPage() {
  const { user } = useAuth()
  const { toast } = useToast()

  const [documents, setDocuments] = useState<DocumentRow[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set())

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await listDocuments()
      setDocuments(rows)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load documents', 'error')
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // US-013: subscribe to Realtime row changes on public.documents so the list
  // reflects backend-driven status transitions (queued → processing → ready
  // / error) without polling. INSERT and UPDATE handlers dedupe by id so
  // optimistic upserts from the upload path stay coherent with the live feed.
  useEffect(() => {
    if (!user) return
    const unsubscribe = subscribeToDocuments(user.id, {
      onInsert: (row) => {
        if (row.deleted_at) return
        setDocuments((prev) =>
          prev.some((d) => d.id === row.id) ? prev : [row, ...prev],
        )
      },
      onUpdate: (row, old) => {
        setDocuments((prev) => {
          if (row.deleted_at) return prev.filter((d) => d.id !== row.id)
          const idx = prev.findIndex((d) => d.id === row.id)
          if (idx === -1) return [row, ...prev]
          const next = prev.slice()
          next[idx] = row
          return next
        })
        // Surface backend ingestion failures at the moment the row flips —
        // only fire on the transition, not on every re-render of an already-
        // errored row that got touched for some other reason.
        if (row.status === 'error' && old.status !== 'error') {
          toast(
            row.error_message
              ? `Ingestion failed (${row.filename}): ${row.error_message}`
              : `Ingestion failed (${row.filename})`,
            'error',
          )
        }
      },
      onDelete: (id) => {
        setDocuments((prev) => prev.filter((d) => d.id !== id))
      },
    })
    return unsubscribe
  }, [user, toast])

  async function handleFiles(files: File[]) {
    if (!user) return

    const accepted: File[] = []
    for (const file of files) {
      if (isAcceptedFile(file)) {
        accepted.push(file)
      } else {
        toast(`Unsupported file: ${file.name}. Only .txt and .md are accepted.`, 'error')
      }
    }
    if (accepted.length === 0) return

    setUploading(true)
    try {
      for (const file of accepted) {
        try {
          const row = await uploadDocument(user.id, file)
          // Optimistic upsert: Realtime will also deliver this row, the
          // subscription's dedupe keeps us from double-rendering.
          setDocuments((prev) =>
            prev.some((d) => d.id === row.id)
              ? prev.map((d) => (d.id === row.id ? row : d))
              : [row, ...prev],
          )
        } catch (e) {
          toast(
            e instanceof Error
              ? `Upload failed (${file.name}): ${e.message}`
              : `Upload failed (${file.name})`,
            'error',
          )
        }
      }
    } finally {
      setUploading(false)
    }
  }

  async function handleDelete(doc: DocumentRow) {
    setDeletingIds((prev) => new Set(prev).add(doc.id))
    try {
      await softDeleteDocument(doc.id)
      setDocuments((prev) => prev.filter((d) => d.id !== doc.id))
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to delete document', 'error')
    } finally {
      setDeletingIds((prev) => {
        const next = new Set(prev)
        next.delete(doc.id)
        return next
      })
    }
  }

  return (
    <div className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 overflow-y-auto px-6 py-8">
        <div className="mb-6">
          <h2 className="text-xl font-semibold">Ingestion</h2>
          <p className="mt-1 text-sm text-neutral-400">
            Upload documents for retrieval. Later modules will chunk and embed them — for now the
            UI persists the file and tracks its state.
          </p>
        </div>

        <div className="mb-6">
          <DropZone onFiles={handleFiles} disabled={uploading} />
          {uploading ? (
            <p className="mt-2 text-xs text-neutral-500">Uploading…</p>
          ) : null}
        </div>

        <DocumentsTable
          documents={documents}
          loading={loading}
          deletingIds={deletingIds}
          onDelete={handleDelete}
        />
      </main>
    </div>
  )
}
