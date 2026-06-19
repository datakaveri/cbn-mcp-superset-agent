import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// `base` controls the path the app is served under. For the production sub-path
// deploy behind the proxy, build with VITE_BASE=/chatbot/. Dev stays at '/'.
const base = process.env.VITE_BASE || '/';

// In dev, the React app runs on Vite's port and proxies the agent's API routes
// to the Flask backend (default :5001). SSE (/run) streams through unbuffered.
const backend = process.env.VITE_BACKEND || 'http://localhost:5001';
const apiRoutes = ['/auth-config', '/run', '/guest-token', '/health'];

// Brand color = CBN crest green (keeps the PWA theme aligned with the logo).
const THEME_COLOR = '#15873d';

export default defineConfig({
  base,
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'logo.png', 'icon-192.png', 'icon-512.png'],
      // SW must not intercept the API routes (SSE especially) — let them hit the network.
      workbox: {
        navigateFallbackDenylist: [/^\/(run|guest-token|auth-config|health)/],
      },
      manifest: {
        name: 'CBN Analytics',
        short_name: 'CBN Analytics',
        description: 'Ask for a chart or dashboard in plain language — built and shown inline.',
        theme_color: THEME_COLOR,
        background_color: '#ffffff',
        display: 'standalone',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      apiRoutes.map((r) => [r, { target: backend, changeOrigin: true }]),
    ),
  },
});
