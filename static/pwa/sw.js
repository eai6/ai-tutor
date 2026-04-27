// Service worker — caches the app shell for instant boot + an offline
// fallback page for navigations that miss the network.
//
// Strategy:
//   - precache: shell HTML (offline fallback) + the manifest + icons
//   - HTML navigations:  network-first, fall back to cached offline page
//   - /static/ assets:   cache-first (immutable; rev via filename if needed)
//   - /media/ + image hosts: cache-first (lesson images sized in MB —
//     ditching them on every page load is brutal on slow connections)
//   - Google Fonts:      stale-while-revalidate (CSS + woff2 files)
//   - /tutor/api/*:      network-only (never cache LLM responses)
//
// Bump CACHE_VERSION whenever the static asset list changes so old SWs
// drop their stale caches on activate.

const CACHE_VERSION = 'v2';
const SHELL_CACHE = `aitutor-shell-${CACHE_VERSION}`;
const STATIC_CACHE = `aitutor-static-${CACHE_VERSION}`;
const MEDIA_CACHE = `aitutor-media-${CACHE_VERSION}`;
const FONTS_CACHE = `aitutor-fonts-${CACHE_VERSION}`;
// Hard cap on cached media so we don't fill the device — basic LRU.
const MEDIA_MAX_ENTRIES = 100;

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
    event.respondWith(cacheFirst(STATIC_CACHE, request));
    return;
  }

  // Lesson media (images, audio). Hit-rate is high because the same
  // images render across many sessions of the same lesson. LRU-trim
  // to MEDIA_MAX_ENTRIES so we don't fill the device.
  if (url.pathname.startsWith('/media/')) {
    event.respondWith(cacheFirstWithLimit(MEDIA_CACHE, request, MEDIA_MAX_ENTRIES));
    return;
  }

  // Google Fonts — stale-while-revalidate. CSS hash bumps automatically
  // when the request URL changes, so revalidation is cheap.
  if (
    url.hostname === 'fonts.googleapis.com' ||
    url.hostname === 'fonts.gstatic.com'
  ) {
    event.respondWith(staleWhileRevalidate(FONTS_CACHE, request));
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

async function cacheFirst(cacheName, request) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    return cached || Response.error();
  }
}

async function cacheFirstWithLimit(cacheName, request, maxEntries) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
      // LRU trim — drop the oldest entries until we're under the cap.
      const keys = await cache.keys();
      if (keys.length > maxEntries) {
        for (let i = 0; i < keys.length - maxEntries; i += 1) {
          await cache.delete(keys[i]);
        }
      }
    }
    return response;
  } catch {
    return Response.error();
  }
}

async function staleWhileRevalidate(cacheName, request) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || fetchPromise;
}
