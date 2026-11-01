import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// Resolve relative to this file so `vite` works no matter the cwd.
const dirname = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  root: dirname,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Dev only: the service owns /api and /api/events on 5177.
      '/api': { target: 'http://127.0.0.1:5177', ws: true },
    },
  },
})
