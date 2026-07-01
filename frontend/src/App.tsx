import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '@/contexts/AuthContext'
import { ProtectedRoute } from '@/components/ProtectedRoute'
import { ToastProvider } from '@/components/ui/toast'
import { LoginPage } from '@/pages/LoginPage'
import { ChatPage } from '@/pages/ChatPage'
import { IngestionPage } from '@/pages/IngestionPage'
import { SupportQueuePage } from '@/pages/SupportQueuePage'
import { AuthCallback } from '@/pages/AuthCallback'

export default function App() {
  return (
    <AuthProvider>
      <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route
            path="/chat"
            element={
              <ProtectedRoute>
                <ChatPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/chat/:threadId"
            element={
              <ProtectedRoute>
                <ChatPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/ingestion"
            element={
              <ProtectedRoute>
                <IngestionPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/support/queue"
            element={
              <ProtectedRoute>
                <SupportQueuePage />
              </ProtectedRoute>
            }
          />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  )
}
