import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { resolveActiveWorkspace } from '@/lib/supportQueue'

const baseNavItems = [
  { to: '/chat', label: 'Chat' },
  { to: '/ingestion', label: 'Ingestion' },
  { to: '/support/queue', label: 'Support queue' },
]

export function AppHeader() {
  const { user, signOut } = useAuth()

  // Support settings is the ADMIN surface (US-090) — show its nav link only to a
  // workspace admin. This is a UX gate; the hard boundary is server-side RLS
  // (the route itself renders an admins-only note for non-admins).
  const [isAdmin, setIsAdmin] = useState(false)
  useEffect(() => {
    let cancelled = false
    resolveActiveWorkspace()
      .then((w) => {
        if (!cancelled) setIsAdmin(w.status === 'resolved' && w.role === 'admin')
      })
      .catch(() => {
        if (!cancelled) setIsAdmin(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const navItems = isAdmin
    ? [...baseNavItems, { to: '/support/settings', label: 'Support settings' }]
    : baseNavItems

  return (
    <header className="flex items-center justify-between border-b border-neutral-800 px-6 py-3">
      <div className="flex items-center gap-6">
        <h1 className="text-lg font-semibold">Agentic RAG</h1>
        <nav className="flex items-center gap-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'rounded-md px-3 py-1.5 text-sm text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100',
                  isActive && 'bg-neutral-800 text-neutral-100',
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="flex items-center gap-3 text-sm">
        <span className="text-neutral-400">{user?.email}</span>
        <Button variant="outline" size="sm" onClick={() => signOut()}>
          Log out
        </Button>
      </div>
    </header>
  )
}
