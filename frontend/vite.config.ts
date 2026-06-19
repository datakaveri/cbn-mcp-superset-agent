import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `base` controls the path the app is served under. For the production sub-path
// deploy behind the proxy, build with VITE_BASE=/chatbot/. Dev stays at '/'.
const base = process.env.VITE_BASE || '/';

// In dev, the React app runs on Vite's port and proxies the agent's API routes
// to the Flask backend (default :5001). SSE (/run) streams through unbuffered.
const backend = process.env.VITE_BACKEND || 'http://localhost:5001';
const apiRoutes = ['/auth-config', '/run', '/guest-token', '/health'];

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      apiRoutes.map((r) => [r, { target: backend, changeOrigin: true }]),
    ),
  },
});
