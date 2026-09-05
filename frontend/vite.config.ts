import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// Defect 2 (2026-09-05 audit, docs/audit_followup_2026-09-05.md):
// Vite's default dev-server behavior is "listen on 5173, and if that's
// taken, silently move to 5174, 5175, ...". The FastAPI CORS allowlist
// (src/autoscreener/api/main.py `allow_origins=[...]`) is a hardcoded list
// containing ONLY http://localhost:5173 and http://127.0.0.1:5173. A
// developer whose 5173 is already occupied (by another `vite` instance, a
// leftover process, anything) would silently get a dev server on 5174 --
// every API request from that tab then fails CORS, which the UI can only
// show as a generic "can't connect" error with no hint that the port is
// the cause. This was live on this machine on 2026-09-05: both 5173 and
// 5174 were simultaneously listening.
//
// `server.port` + `strictPort: true` turns the silent drift into a loud,
// immediate startup failure ("Port 5173 is already in use") instead. If
// you hit that: find and stop whatever is already holding 5173
// (`netstat -ano | findstr :5173` on Windows, `lsof -i :5173` elsewhere)
// rather than letting -- or asking -- Vite to move to another port; the
// API will reject that origin no matter which port it silently picks.
//
// These two ports are two independently hardcoded literals with no shared
// source of truth across the Python/TypeScript boundary -- if this port
// ever needs to change, you MUST update BOTH this file's `server.port`
// AND api/main.py's `allow_origins` CORS list together, or you reproduce
// this exact defect. Each file's comment points at the other for this
// reason.
export default defineConfig({
  plugins: [react()],
  server: {
    // Keep this in sync with `allow_origins` in
    // src/autoscreener/api/main.py -- see that file's CORS comment.
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
  },
})
