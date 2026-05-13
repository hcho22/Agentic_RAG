// US-025: per-message tool-attribution badges + collapsible details panel.
//
// Renders one button per ToolInvocation attached to an assistant turn (📄
// docs / 🗄️ SQL / 🌐 web). Clicking a button toggles a panel below the
// message bubble that shows the matching sources — chunk previews for
// `search_documents`, the generated SQL + a small results table for
// `query_database`, and a clickable URL list for `web_search`.
//
// US-028: `spawn_document_agent` invocations render as a hierarchical tree
// — the spawning tool call (🤖 Sub-agent) is the root, with the sub-agent's
// chunk reads / reasoning / finalize summary nested as children. The tree
// is collapsible per node so users can drill in without overwhelming the
// thread on summary turns that read many chunks.

import { useState } from 'react'
import { cn } from '@/lib/utils'
import type {
  PlanQueryArgs,
  PlanQueryResultPayload,
  PlanSpec,
  QueryDatabaseResultPayload,
  SearchDocumentsResult,
  SearchDocumentsResultPayload,
  SpawnDocumentAgentArgs,
  SpawnDocumentAgentResultPayload,
  SqlSearchArgs,
  SqlSearchResultPayload,
  SubAgentActivityEntry,
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
  // US-030: plan_query (🧭 Plan) precedes sql_search (🗄️ SQL) in the
  // trace; we reuse the SQL icon for both Module 7's query_database and
  // Module 9's sql_search since the underlying card renders the same shape
  // (SQL string + result table).
  plan_query: { icon: '🧭', label: 'Plan' },
  sql_search: { icon: '🗄️', label: 'SQL' },
  web_search: { icon: '🌐', label: 'Web' },
  spawn_document_agent: { icon: '🤖', label: 'Sub-agent' },
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
  if (inv.kind === 'plan_query') {
    // Show the metric count for matched plans, undefined otherwise.
    // no_match plans surface as the badge without a subscript.
    const r = inv.result
    if (r && 'status' in r && r.status === 'matched') {
      return r.plan?.metrics?.length ?? null
    }
    return null
  }
  if (inv.kind === 'sql_search') {
    return inv.result?.row_count ?? inv.result?.rows?.length ?? null
  }
  if (inv.kind === 'web_search') {
    return inv.result?.results?.length ?? inv.result?.count ?? null
  }
  if (inv.kind === 'spawn_document_agent') {
    // US-028: badge count = number of sub-agent activity steps (reads +
    // reasoning + finalize). Reads alone would undercount; the full step
    // count is what the user sees when they expand the tree.
    return inv.result?.activity?.length ?? null
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
  if (invocation.kind === 'plan_query') {
    return <PlanQueryDetails args={invocation.args} result={invocation.result} />
  }
  if (invocation.kind === 'sql_search') {
    return <SqlSearchDetails args={invocation.args} result={invocation.result} />
  }
  if (invocation.kind === 'web_search') {
    return <WebSearchDetails result={invocation.result} />
  }
  if (invocation.kind === 'spawn_document_agent') {
    return <SpawnDocumentAgentDetails args={invocation.args} result={invocation.result} />
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

// US-030: plan_query details. Renders the structured plan (metrics /
// dimensions / filters as chips, time grain badge) or the no_match reason
// + suggested fallback. The compiled SQL appears in the adjacent sql_search
// card; this panel is intentionally compact — its job is to show *what the
// planner decided*, not run a query.
function PlanQueryDetails({
  args,
  result,
}: {
  args: PlanQueryArgs
  result: PlanQueryResultPayload | null
}) {
  if (!result || ('error' in result && result.error)) {
    return <ErrorRow message={(result && 'error' in result && result.error) || 'No result captured.'} />
  }

  if ('status' in result && result.status === 'no_match') {
    return (
      <div className="space-y-1">
        {args.question && (
          <div className="text-neutral-400">
            question: <code className="text-neutral-200">{args.question}</code>
          </div>
        )}
        <div className="text-amber-300">no_match — {result.reason}</div>
        <div className="text-neutral-500">
          suggested_fallback: <code className="text-neutral-300">{result.suggested_fallback}</code>
        </div>
      </div>
    )
  }

  if ('status' in result && result.status === 'matched') {
    return <PlanSpecBlock question={args.question} plan={result.plan} />
  }

  return <ErrorRow message="Unrecognised plan_query result shape." />
}

function PlanSpecBlock({
  question,
  plan,
}: {
  question?: string
  plan: PlanSpec
}) {
  const metrics = plan.metrics ?? []
  const dimensions = plan.dimensions ?? []
  const filters = plan.filters ?? []
  return (
    <div className="space-y-2">
      {question && (
        <div className="text-neutral-400">
          question: <code className="text-neutral-200">{question}</code>
        </div>
      )}
      <PlanChipRow label="metrics" tone="emerald" items={metrics} />
      {dimensions.length > 0 && (
        <PlanChipRow label="dimensions" tone="sky" items={dimensions} />
      )}
      {plan.time_grain && (
        <div className="text-neutral-400">
          time grain:{' '}
          <code className="text-neutral-200">{plan.time_grain}</code>
        </div>
      )}
      {filters.length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] uppercase tracking-wide text-neutral-500">filters</div>
          <ul className="space-y-1">
            {filters.map((f, i) => (
              <li key={i} className="rounded border border-neutral-800 bg-neutral-950/60 px-2 py-1">
                <code className="text-neutral-200">{f.dimension}</code>{' '}
                <span className="text-neutral-500">{f.op}</span>{' '}
                <code className="text-neutral-300">{formatFilterValue(f.value)}</code>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function PlanChipRow({
  label,
  tone,
  items,
}: {
  label: string
  tone: 'emerald' | 'sky'
  items: string[]
}) {
  if (items.length === 0) return null
  const toneCls =
    tone === 'emerald'
      ? 'border-emerald-700 bg-emerald-950/40 text-emerald-200'
      : 'border-sky-700 bg-sky-950/40 text-sky-200'
  return (
    <div className="flex flex-wrap items-baseline gap-1.5">
      <span className="text-[11px] uppercase tracking-wide text-neutral-500">{label}:</span>
      {items.map((it) => (
        <span
          key={it}
          className={cn('inline-flex rounded-full border px-2 py-0.5 text-[11px]', toneCls)}
        >
          {it}
        </span>
      ))}
    </div>
  )
}

function formatFilterValue(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return JSON.stringify(v)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// US-030: sql_search uses the same result shape as query_database; reuse
// the existing details panel. We accept `args` only to satisfy the union
// types — the panel itself just renders the compiled SQL + rows.
function SqlSearchDetails({
  args: _args,
  result,
}: {
  args: SqlSearchArgs
  result: SqlSearchResultPayload | null
}) {
  return <QueryDatabaseDetails result={result} />
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

// US-028: hierarchical tool-call display for `spawn_document_agent`.
//
// Renders the sub-agent run as a tree: the spawning call (already shown as
// the badge above) is the root, with the sub-agent's activity log as a
// nested child list. Each activity entry collapses by kind (read / reason /
// finalize / error) so the user can scan a long run without the tree
// overwhelming the chat. The final summary is surfaced at the bottom in
// addition to being in the activity log — it's the most useful cue when the
// user just wants "what did the sub-agent conclude?".
function SpawnDocumentAgentDetails({
  args,
  result,
}: {
  args: SpawnDocumentAgentArgs
  result: SpawnDocumentAgentResultPayload | null
}) {
  if (!result || result.error) {
    return <ErrorRow message={result?.error ?? 'No result captured.'} />
  }
  const activity = result.activity ?? []
  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-0.5 text-neutral-400">
        {result.filename && (
          <div>
            document: <code className="text-neutral-200">{result.filename}</code>
          </div>
        )}
        {args.task && (
          <div className="line-clamp-2">
            task: <span className="text-neutral-300">{args.task}</span>
          </div>
        )}
        <div className="text-neutral-500">
          {result.iterations ?? 0} iteration
          {result.iterations === 1 ? '' : 's'} ·{' '}
          {activity.filter((a) => a.kind === 'read').length} read
          {activity.filter((a) => a.kind === 'read').length === 1 ? '' : 's'}
          {result.chunks_total !== undefined && ` of ${result.chunks_total} chunks`}
          {result.truncated && ' · truncated (iteration cap reached)'}
        </div>
      </div>
      <div className="rounded border-l-2 border-neutral-700 pl-2">
        <SubAgentActivityTree activity={activity} />
      </div>
      {result.summary && (
        <div className="rounded border border-neutral-800 bg-neutral-950/60 p-2">
          <div className="mb-1 text-[11px] uppercase tracking-wide text-neutral-500">
            Summary
          </div>
          <div className="whitespace-pre-wrap text-neutral-200">
            {result.summary}
          </div>
        </div>
      )}
    </div>
  )
}

function SubAgentActivityTree({ activity }: { activity: SubAgentActivityEntry[] }) {
  if (activity.length === 0) {
    return <div className="text-neutral-500">No sub-agent activity recorded.</div>
  }
  return (
    <ul className="space-y-1">
      {activity.map((entry, i) => (
        <SubAgentActivityNode key={i} entry={entry} index={i} />
      ))}
    </ul>
  )
}

function SubAgentActivityNode({
  entry,
  index,
}: {
  entry: SubAgentActivityEntry
  index: number
}) {
  const [open, setOpen] = useState(false)
  const meta = activityMeta(entry)
  const expandable =
    Boolean(entry.preview && entry.preview.length > 0) ||
    Boolean(entry.text && entry.text.length > 0) ||
    Boolean(entry.summary && entry.summary.length > 0)

  return (
    <li>
      <button
        type="button"
        onClick={() => expandable && setOpen((v) => !v)}
        aria-expanded={expandable ? open : undefined}
        className={cn(
          'flex w-full items-baseline gap-2 text-left',
          expandable && 'cursor-pointer hover:text-neutral-100',
          !expandable && 'cursor-default',
          meta.tone,
        )}
      >
        <span className="w-5 flex-shrink-0 text-neutral-500">
          {expandable ? (open ? '▾' : '▸') : '·'}
        </span>
        <span className="text-neutral-500">{index + 1}.</span>
        <span>{meta.icon}</span>
        <span className="font-mono text-[11px] uppercase tracking-wide">
          {meta.label}
        </span>
        {meta.subline && (
          <span className="truncate text-neutral-400">{meta.subline}</span>
        )}
      </button>
      {open && expandable && (
        <div className="mt-1 ml-7 whitespace-pre-wrap rounded bg-neutral-950/60 p-2 text-neutral-300">
          {entry.kind === 'finalize'
            ? entry.summary ?? ''
            : entry.preview ?? entry.text ?? ''}
        </div>
      )}
    </li>
  )
}

function activityMeta(entry: SubAgentActivityEntry): {
  icon: string
  label: string
  subline: string
  tone: string
} {
  if (entry.kind === 'read') {
    return {
      icon: '📖',
      label: 'read',
      subline:
        entry.chunk_index !== null && entry.chunk_index !== undefined
          ? `chunk #${entry.chunk_index}${entry.preview ? ' — ' + entry.preview.slice(0, 80) : ''}`
          : entry.preview ?? '',
      tone: 'text-neutral-300',
    }
  }
  if (entry.kind === 'reason') {
    return {
      icon: '💭',
      label: 'reason',
      subline: (entry.text ?? '').slice(0, 120),
      tone: 'text-neutral-300',
    }
  }
  if (entry.kind === 'finalize') {
    return {
      icon: '✅',
      label: 'finalize',
      subline: (entry.summary ?? '').slice(0, 120),
      tone: 'text-emerald-300',
    }
  }
  return {
    icon: '⚠️',
    label: 'error',
    subline: entry.text ?? '',
    tone: 'text-red-300',
  }
}
