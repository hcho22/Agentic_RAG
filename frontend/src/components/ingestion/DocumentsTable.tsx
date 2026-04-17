import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { DocumentRow, DocumentStatus } from '@/lib/ingestion'

type Props = {
  documents: DocumentRow[]
  loading: boolean
  deletingIds: Set<string>
  onDelete: (doc: DocumentRow) => void
}

const STATUS_STYLES: Record<DocumentStatus, string> = {
  queued: 'bg-neutral-800 text-neutral-300',
  processing: 'bg-blue-950/60 text-blue-200',
  ready: 'bg-emerald-950/60 text-emerald-200',
  error: 'bg-red-950/60 text-red-200',
}

export function DocumentsTable({ documents, loading, deletingIds, onDelete }: Props) {
  if (loading) {
    return <p className="py-6 text-sm text-neutral-500">Loading documents…</p>
  }
  if (documents.length === 0) {
    return <p className="py-6 text-sm text-neutral-500">No documents yet.</p>
  }

  return (
    <div className="overflow-hidden rounded-lg border border-neutral-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-900 text-xs uppercase tracking-wide text-neutral-400">
          <tr>
            <th className="px-4 py-2 font-medium">Filename</th>
            <th className="px-4 py-2 font-medium">Status</th>
            <th className="px-4 py-2 font-medium">Chunks</th>
            <th className="px-4 py-2 font-medium">Uploaded</th>
            <th className="px-4 py-2 font-medium" aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {documents.map((doc) => (
            <tr key={doc.id} className="border-t border-neutral-800">
              <td className="px-4 py-2 text-neutral-100">
                <div className="truncate">{doc.filename}</div>
                {doc.status === 'error' && doc.error_message ? (
                  <div className="mt-0.5 truncate text-xs text-red-300">{doc.error_message}</div>
                ) : null}
              </td>
              <td className="px-4 py-2">
                <span
                  className={cn(
                    'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
                    STATUS_STYLES[doc.status],
                  )}
                >
                  {doc.status}
                </span>
              </td>
              <td className="px-4 py-2 tabular-nums text-neutral-300">{doc.chunks_count}</td>
              <td className="px-4 py-2 text-neutral-400">
                {new Date(doc.uploaded_at).toLocaleString()}
              </td>
              <td className="px-4 py-2 text-right">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onDelete(doc)}
                  disabled={deletingIds.has(doc.id)}
                >
                  {deletingIds.has(doc.id) ? 'Deleting…' : 'Delete'}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
