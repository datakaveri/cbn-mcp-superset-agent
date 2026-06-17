/* Service worker for the Superset MCP Agent PWA.
 * Caches the static shell for offline launch / installability.
 * Never caches auth or API traffic (/run, /health, /auth-config) or
 * cross-origin requests (Keycloak, OpenAI, Superset).
 */
const CACHE = 'mcp-agent-v2';
// Relative to the SW location, so it works under a sub-path (e.g. /chatbot/).
const SHELL = [
  './',
  'index.html',
  'manifest.webmanifest',
  'logo.png',
  'favicon.ico',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;                       // leave POST /run alone

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;            // don't touch Keycloak/OpenAI/Superset
  // Skip auth/API/SW regardless of any path prefix (/chatbot/auth-config, etc.)
  if (/\/(run|health|auth-config|service-worker\.js)$/.test(url.pathname)) return;

  // Cache-first for the static shell, with a network refresh in the background.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(request, copy));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
