// US-025: per-message tool-attribution badges + collapsible details panel.
//
// Renders one button per ToolInvocation attached to an assistant turn (📄
// docs / 🗄️ SQL / 🌐 web). Clicking a button toggles a panel below the
// message bubble that shows the matching sources — chunk previews for
// `search_documents`, the generated SQL + a small results table for
// `query_database`, and a clickable URL list for `web_search`.

import { useState } from 'react'
import { cn } from '@/lib/utils'
import type {
  QueryDatabaseResultPayload,
  SearchDocumentsResult,
  SearchDocumentsResultPayload,
  ToolInvocation,
  WebSearchHit,
  WebSearchResultPayload,
} from '@/lib/toolInvocations'

type Props = {
  invocations: ToolInvocation[]
}

const TOOL_LABELS: Record<string, { icon: string; label: string }> = {
  search_documents: { icon: '📄', label: 'Docs' },
  query_database: { icon: '🗄️', label: 'SQL' },
  web_search: { icon: '🌐', label: 'Web' },
}

export function ToolAttribution({ invocations }: Props) {
  const [openId, setOpenId] = useState<string | null>(null)
  if (invocations.length === 0) return null

  const open = invocations.find((i) => i.toolCallId === openId) ?? null

  return (
    <div className="mt-2 flex flex-col items-start gap-2">
      <div className="flex flex-wrap gap-1.5">
        {invocations.map((inv) => {
          const meta =
            inv.kind === 'unknown'
              ? { icon: '🛠️', label: inv.name || 'tool' }
              : TOOL_LABELS[inv.kind]
          if (!meta) return null
          const active = inv.toolCallId === openId
          const errored = invocationHasError(inv)
          return (
            <button
              key={inv.toolCallId}
              type="button"
              onClick={() => setOpenId(active ? null : inv.toolCallId)}
              aria-expanded={active}
              aria-label={`${meta.label} tool details`}
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition',
                'border-neutral-700 bg-neutral-900 text-neutral-300 hover:border-neutral-500 hover:text-neutral-100',
                active && 'border-neutral-400 bg-neutral-800 text-neutral-100',
                errored && 'border-red-700 text-red-300',
              )}
            >
              <span>{meta.icon}</span>
              <span>{meta.label}</span>
              {invocationCount(inv) !== null && (
                <span className="opacity-70">· {invocationCount(inv)}</span>
              )}
            </button>
          )
        })}
      </div>
      {open && (
        <div className="w-full rounded border border-neutral-800 bg-neutral-900/60 p-3 text-xs text-neutral-300">
          <ToolDetails invocation={open} />
        </div>
      )}
    </div>
  )
}

function invocationHasError(inv: ToolInvocation): boolean {
  if (inv.kind === 'unknown') return false
  const r = inv.result as { error?: unknown } | null
  return Boolean(r && typeof r.error === 'string' && r.error.length > 0)
}

// Number to display next to the badge (e.g. 5 chunks, 100 rows). Returns
// null when there's no useful count for that tool kind.
function invocationCount(inv: ToolInvocation): number | null {
  if (inv.kind === 'search_documents') {
    return inv.result?.results?.length ?? inv.result?.count ?? null
  }
  if (inv.kind === 'query_database') {
    return inv.result?.row_count ?? inv.result?.rows?.length ?? null
  }
  if (inv.kind === 'web_search') {
    return inv.result?.results?.length ?? inv.result?.count ?? null
  }
  return null
}

function ToolDetails({ invocation }: { invocation: ToolInvocation }) {
  if (invocation.kind === 'search_documents') {
    return <SearchDocumentsDetails args={invocation.args} result={invocation.result} />
  }
  if (invocation.kind === 'query_database') {
    return <QueryDatabaseDetails result={invocation.result} />
  }
  if (invocation.kind === 'web_search') {
    return <WebSearchDetails result={invocation.result} />
  }
  return (
    <pre className="overflow-x-auto whitespace-pre-wrap break-words text-[11px] text-neutral-400">
      {JSON.stringify({ args: invocation.args, result: invocation.result }, null, 2)}
    </pre>
  )
}

function SearchDocumentsDetails({
  args,
  result,
}: {
  args: { query?: string }
  result: SearchDocumentsResultPayload | null
}) {
  if (!result || result.error) {
    return <ErrorRow message={result?.error ?? 'No result captured.'} />
  }
  const chunks = result.results ?? []
  if (chunks.length === 0) {
    return (
      <div className="text-neutral-400">
        No matching chunks for query <code className="text-neutral-200">{args.query ?? ''}</code>.
      </div>
    )
  }
  return (
    <div className="space-y-2">
      {args.query && (
        <div className="text-neutral-400">
          query: <code className="text-neutral-200">{args.query}</code>
          {result.retrieval_mode && (
            <span className="ml-2 text-neutral-500">
              · {result.retrieval_mode}
              {result.reranker && result.reranker !== 'none' ? ` + ${result.reranker}` : ''}
            </span>
          )}
        </div>
      )}
      <ul className="space-y-1.5">
        {chunks.map((c) => (
          <ChunkPreview key={c.id} chunk={c} />
        ))}
      </ul>
    </div>
  )
}

function ChunkPreview({ chunk }: { chunk: SearchDocumentsResult }) {
  return (
    <li className="rounded border border-neutral-800 bg-neutral-950/60 p-2">
      <div className="mb-1 flex items-baseline justify-between gap-2 text-[11px] text-neutral-500">
        <span className="truncate text-neutral-300">{chunk.filename}</span>
        <span>
          chunk #{chunk.chunk_index} · score {chunk.similarity.toFixed(3)}
        </span>
      </div>
      <div className="line-clamp-3 whitespace-pre-wrap text-neutral-300">{chunk.content}</div>
    </li>
  )
}

function QueryDatabaseDetails({
  result,
}: {
  result: QueryDatabaseResultPayload | null
}) {
  if (!result || result.error) {
    return <ErrorRow message={result?.error ?? 'No result captured.'} />
  }
  const rows = result.rows ?? []
  const columns = result.columns ?? (rows[0] ? Object.keys(rows[0]) : [])
  return (
    <div className="space-y-2">
      {result.sql && (
        <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-neutral-950 p-2 font-mono text-[11px] text-neutral-200">
          {result.sql}
        </pre>
      )}
      {rows.length > 0 ? (
        <div className="overflow-x-auto rounded border border-neutral-800">
          <table className="w-full text-[11px]">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                {columns.map((c) => (
                  <th key={c} className="px-2 py-1 text-left font-medium">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 25).map((row, i) => (
                <tr key={i} className="border-t border-neutral-800">
                  {columns.map((c) => (
                    <td key={c} className="px-2 py-1 align-top text-neutral-300">
                      {formatCell(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {(rows.length > 25 || result.truncated) && (
            <div className="border-t border-neutral-800 px-2 py-1 text-[11px] text-neutral-500">
              {result.truncated ? 'showing first 25 of truncated result set' : `showing 25 of ${rows.length} rows`}
            </div>
          )}
        </div>
      ) : (
        <div className="text-neutral-400">Query returned no rows.</div>
      )}
    </div>
  )
}

function WebSearchDetails({
  result,
}: {
  result: WebSearchResultPayload | null
}) {
  if (!result || result.error) {
    return <ErrorRow message={result?.error ?? 'No result captured.'} />
  }
  const hits = result.results ?? []
  if (hits.length === 0) {
    return <div className="text-neutral-400">No web results.</div>
  }
  return (
    <ul className="space-y-1.5">
      {hits.map((hit) => (
        <WebSearchHitRow key={hit.url} hit={hit} />
      ))}
    </ul>
  )
}

function WebSearchHitRow({ hit }: { hit: WebSearchHit }) {
  return (
    <li className="rounded border border-neutral-800 bg-neutral-950/60 p-2">
      <a
        href={hit.url}
        target="_blank"
        rel="noopener noreferrer"
        className="block truncate text-neutral-100 underline-offset-2 hover:underline"
      >
        {hit.title || hit.url}
      </a>
      <div className="truncate text-[11px] text-neutral-500">{hit.url}</div>
      {hit.snippet && (
        <div className="mt-1 line-clamp-3 text-neutral-300">{hit.snippet}</div>
      )}
    </li>
  )
}

function ErrorRow({ message }: { message: string }) {
  return <div className="text-red-400">{message}</div>
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
