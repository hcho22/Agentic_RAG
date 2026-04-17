import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'

export function AuthCallback() {
  const { session, loading } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    if (!loading) navigate(session ? '/chat' : '/login', { replace: true })
  }, [session, loading, navigate])

  return (
    <div className="flex h-screen items-center justify-center text-neutral-400">
      Signing you in…
    </div>
  )
}
