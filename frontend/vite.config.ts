import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      // Multi-page build: the authenticated admin app (index.html) AND the public
      // support-widget iframe shell (widget.html, US-083). The widget is a
      // separate entry/bundle served from the kit's own origin — it never loads
      // the admin app. `dist/widget.html` is a real static file, so the SPA
      // rewrite in vercel.json (which only fires when no file matches) leaves it
      // and `widget.js` (a public/ asset) reachable directly.
      input: {
        main: path.resolve(__dirname, 'index.html'),
        widget: path.resolve(__dirname, 'widget.html'),
      },
    },
  },
})
