import { NavLink } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const navItems = [
  { to: '/chat', label: 'Chat' },
  { to: '/ingestion', label: 'Ingestion' },
  { to: '/support/queue', label: 'Support queue' },
] as const

export function AppHeader() {
  const { user, signOut } = useAuth()

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
