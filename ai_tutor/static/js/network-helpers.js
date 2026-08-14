// Network resilience helpers for low-connectivity / mobile contexts.
// Loaded once from base.html; everything attaches to window.NetHelpers.
//
// What's in here:
//   - NetHelpers.isOnline()           — current connectivity (navigator.onLine + connection events)
//   - NetHelpers.onNetworkChange(fn)  — subscribe to online/offline transitions; returns unsubscribe
//   - NetHelpers.fetchWithRetry(url, opts) — retries 5xx + network errors with exponential backoff
//                                            (pass retryOnNetworkError:false for non-idempotent POSTs)
//   - NetHelpers.installOfflineBanner() — wires the #offline-banner element to NetworkChange
//   - NetHelpers.registerServiceWorker() — registers /static/pwa/sw.js (call once at app start)

(function () {
  'use strict';

  const listeners = new Set();
  let lastState = navigator.onLine;

  function emit(state) {
    if (state === lastState) return;
    lastState = state;
    for (const fn of listeners) {
      try { fn(state); } catch (e) { console.error('[net]', e); }
    }
  }

  window.addEventListener('online', () => emit(true));
  window.addEventListener('offline', () => emit(false));

  function isOnline() {
    return navigator.onLine;
  }

  function onNetworkChange(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  /**
   * fetch with exponential-backoff retry on transient failures.
   *
   * Retries on:
   *   - network error (TypeError thrown by fetch) / timeout abort
   *       — ONLY when retryOnNetworkError is true (the default)
   *   - 502 / 503 / 504
   *   - 408 / 429 (with Retry-After when present)
   *
   * Does NOT retry on 4xx (other than 408/429). Those say the request
   * itself was wrong, so repeating it unchanged cannot help.
   *
   * Options (extend RequestInit):
   *   retries: number   — max attempts (default 3)
   *   timeoutMs: number — per-attempt timeout (default 60s; tutor calls override to 120s+)
   *   onRetry: (attempt, delayMs) => void
   *   retryOnNetworkError: boolean — default true. Set FALSE for
   *     non-idempotent POSTs. This distinction is load-bearing:
   *
   *     A 502/503/504 is the server saying it did NOT process the
   *     request, so replaying is safe.
   *
   *     A network error — or an AbortError from our own timeout —
   *     says nothing about whether the server processed it. The
   *     request may have arrived, run to completion, and only the
   *     *response* got lost. Replaying re-runs the work.
   *
   *     For a tutoring turn on the offline Jetson that is not
   *     academic: a turn is three serialized local LLM calls at
   *     ~16 tok/s, so exceeding the 120s client timeout is ordinary
   *     rather than exceptional. The retry then ran a second full
   *     turn against the same session while the first was still
   *     running — double-persisting turns and double-advancing the
   *     step. Hence retryOnNetworkError: false on the tutor's
   *     respond call.
   */
  async function fetchWithRetry(url, opts = {}) {
    const {
      retries = 3,
      timeoutMs = 60_000,
      onRetry,
      retryOnNetworkError = true,
      ...fetchOpts
    } = opts;

    let attempt = 0;
    let lastError;

    while (attempt <= retries) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, { ...fetchOpts, signal: controller.signal });
        clearTimeout(timer);

        // Retry only specific transient codes.
        if ([408, 429, 502, 503, 504].includes(response.status) && attempt < retries) {
          const retryAfter = parseRetryAfter(response.headers.get('retry-after'));
          const delay = retryAfter ?? backoffDelay(attempt);
          onRetry?.(attempt + 1, delay);
          await sleep(delay);
          attempt += 1;
          continue;
        }
        return response;
      } catch (err) {
        clearTimeout(timer);
        lastError = err;
        const isNetwork = err instanceof TypeError || err.name === 'AbortError';
        if (!isNetwork || !retryOnNetworkError || attempt >= retries) throw err;
        const delay = backoffDelay(attempt);
        onRetry?.(attempt + 1, delay);
        await sleep(delay);
        attempt += 1;
      }
    }
    throw lastError ?? new Error('fetchWithRetry exhausted');
  }

  function backoffDelay(attempt) {
    // 0.4s, 0.9s, 2s, with ~25% jitter.
    const base = Math.pow(2, attempt) * 400;
    const jitter = Math.random() * 0.25 * base;
    return Math.min(base + jitter, 8000);
  }

  function parseRetryAfter(value) {
    if (!value) return null;
    const seconds = Number(value);
    if (!Number.isNaN(seconds)) return seconds * 1000;
    const date = Date.parse(value);
    if (!Number.isNaN(date)) return Math.max(0, date - Date.now());
    return null;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function installOfflineBanner() {
    const banner = document.getElementById('offline-banner');
    if (!banner) return;
    const update = (online) => {
      banner.hidden = online;
      banner.setAttribute('aria-hidden', String(online));
    };
    update(isOnline());
    onNetworkChange(update);
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return null;
    try {
      // The SW lives at /sw.js (a Django view that re-serves the
      // file from /static/pwa/sw.js with Service-Worker-Allowed: /).
      // Registering at site root means scope: '/' is honored.
      const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      return reg;
    } catch (err) {
      console.warn('[sw] registration failed:', err);
      return null;
    }
  }

  window.NetHelpers = {
    isOnline,
    onNetworkChange,
    fetchWithRetry,
    installOfflineBanner,
    registerServiceWorker,
  };
})();
