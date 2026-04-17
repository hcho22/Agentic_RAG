import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { session, loading } = useAuth()
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-neutral-400">Loading…</div>
    )
  }
  if (!session) return <Navigate to="/login" replace />
  return <>{children}</>
}
