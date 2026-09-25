/* Service worker for the app shell ONLY.
 *
 * Served from /sw.js (the root) so its scope is the whole app — a worker under
 * /static/ could never control /.
 *
 * Caching contract (see the PWA ticket):
 *  - The shell (this page, the manifest, the icons) is precached so the app opens
 *    instantly offline.
 *  - EVERYTHING else — robot status, the map, rooms, consumables, schedules, and every
 *    command POST — is deliberately not handled here, so it goes straight to the
 *    network and is never cached or replayed. A stale map or a queued clean command
 *    firing later is worse than no offline mode at all.
 */

const CACHE = "roborock-shell-v4-__SHELL_VERSION__";

const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});

self.addEventListener("activate", (e) => {
  // Versioned cache: drop every previous version so a stale shell cannot survive a deploy.
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// The page tells the waiting (updated) worker to take over when the user taps Reload.
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // commands: network-only, never intercepted
  const url = new URL(req.url);
  // Only exact shell paths — a navigation to an API GET route (/status, /map.png in a
  // new tab) is also mode "navigate", and caching it under "/" would poison the shell.
  const isShell = url.origin === self.location.origin && SHELL.includes(url.pathname);
  if (!isShell) return; // live robot/scheduler state: network-only, never cached

  // Shell: network-first (never stale while online), cache fallback (instant offline).
  const cacheKey = url.pathname;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(cacheKey, copy));
        }
        return res;
      })
      .catch(() => caches.match(cacheKey).then((hit) => hit || Response.error()))
  );
});
