import { NavLink } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { ThreadRow } from '@/lib/chat'

type Props = {
  threads: ThreadRow[]
  loading: boolean
  onNewThread: () => void
  creating: boolean
  onDeleteThread: (threadId: string) => void
  deletingThreadId: string | null
}

export function ThreadList({
  threads,
  loading,
  onNewThread,
  creating,
  onDeleteThread,
  deletingThreadId,
}: Props) {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-neutral-800 bg-neutral-950">
      <div className="p-3">
        <Button className="w-full" onClick={onNewThread} disabled={creating}>
          {creating ? 'Creating…' : 'New thread'}
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {loading ? (
          <p className="px-2 py-4 text-xs text-neutral-500">Loading threads…</p>
        ) : threads.length === 0 ? (
          <p className="px-2 py-4 text-xs text-neutral-500">No threads yet.</p>
        ) : (
          <ul className="space-y-1">
            {threads.map((t) => {
              const isDeleting = deletingThreadId === t.id
              return (
                <li key={t.id} className="relative">
                  <NavLink
                    to={`/chat/${t.id}`}
                    className={({ isActive }) =>
                      cn(
                        'block truncate rounded-md py-2 pl-3 pr-9 text-sm text-neutral-300 hover:bg-neutral-800',
                        isActive && 'bg-neutral-800 text-neutral-100',
                        isDeleting && 'opacity-50',
                      )
                    }
                  >
                    {t.title ?? 'Untitled'}
                  </NavLink>
                  <button
                    type="button"
                    aria-label={`Delete thread ${t.title ?? 'Untitled'}`}
                    title="Delete thread"
                    disabled={isDeleting}
                    onClick={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      onDeleteThread(t.id)
                    }}
                    className="absolute right-1 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded text-lg leading-none text-neutral-500 hover:bg-neutral-700 hover:text-neutral-100 focus:outline-none focus:ring-2 focus:ring-neutral-500 disabled:pointer-events-none disabled:opacity-50"
                  >
                    ×
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </aside>
  )
}
