import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { WidgetApp } from './WidgetApp'
import './widget.css'

// US-083: entry point for the cross-origin iframe widget shell. This bundle is a
// SEPARATE Vite entry (widget.html) from the admin app (index.html) — it never
// imports the authenticated app, supabase-js, or the dark theme. It is served
// from the kit's own origin and embedded as an iframe by frontend/public/widget.js.

createRoot(document.getElementById('widget-root')!).render(
  <StrictMode>
    <WidgetApp />
  </StrictMode>,
)
