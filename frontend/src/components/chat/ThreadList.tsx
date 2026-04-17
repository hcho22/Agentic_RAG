import { NavLink } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { ThreadRow } from '@/lib/chat'

type Props = {
  threads: ThreadRow[]
  loading: boolean
  onNewThread: () => void
  creating: boolean
}

export function ThreadList({ threads, loading, onNewThread, creating }: Props) {
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
            {threads.map((t) => (
              <li key={t.id}>
                <NavLink
                  to={`/chat/${t.id}`}
                  className={({ isActive }) =>
                    cn(
                      'block truncate rounded-md px-3 py-2 text-sm text-neutral-300 hover:bg-neutral-800',
                      isActive && 'bg-neutral-800 text-neutral-100',
                    )
                  }
                >
                  {t.title ?? 'Untitled'}
                </NavLink>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  )
}
