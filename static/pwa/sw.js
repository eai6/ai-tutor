// Service worker — caches the app shell for instant boot + an offline
// fallback page for navigations that miss the network.
//
// Strategy:
//   - precache: shell HTML (offline fallback) + the manifest + icons
//   - HTML navigations:  network-first, fall back to cached offline page
//   - /static/ assets:   cache-first (immutable; rev via filename if needed)
//   - /tutor/api/*:      network-only (never cache LLM responses)
//
// Bump CACHE_VERSION whenever the static asset list changes so old SWs
// drop their stale caches on activate.

const CACHE_VERSION = 'v1';
const SHELL_CACHE = `aitutor-shell-${CACHE_VERSION}`;
const STATIC_CACHE = `aitutor-static-${CACHE_VERSION}`;

const SHELL_URLS = [
  '/static/pwa/manifest.webmanifest',
  '/static/pwa/icon.svg',
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png',
  '/static/pwa/apple-touch-icon.png',
  '/static/pwa/offline.html',
  '/static/js/network-helpers.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => !k.endsWith(`-${CACHE_VERSION}`))
          .map((k) => caches.delete(k)),
      ),
    ).then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Don't cache LLM / API calls — they must hit the network so the
  // tutor's response is real.
  if (url.pathname.startsWith('/tutor/api/') || url.pathname.startsWith('/api/')) {
    return; // default network handling
  }

  // Static files — cache-first. WhiteNoise hashes filenames so stale
  // entries are naturally invalidated by URL change.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(STATIC_CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        if (cached) return cached;
        try {
          const response = await fetch(request);
          if (response.ok) cache.put(request, response.clone());
          return response;
        } catch {
          return cached || Response.error();
        }
      }),
    );
    return;
  }

  // HTML navigations — network-first. If we're offline and the user
  // navigates somewhere we don't have, serve the offline fallback.
  if (request.mode === 'navigate' || request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      fetch(request).catch(async () => {
        const cache = await caches.open(SHELL_CACHE);
        return (
          (await cache.match('/static/pwa/offline.html')) ||
          new Response('Offline', { status: 503, statusText: 'Offline' })
        );
      }),
    );
  }
});
