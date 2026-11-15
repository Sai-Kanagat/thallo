// Offline shell for read-only views. Only GET is ever handled; every POST, PUT, PATCH and DELETE
// goes straight to the network, so an approval, an edit or a change made offline fails and is never
// queued or replayed. Cached API reads are limited to the deck list and the week plan; a review card
// is never served from cache (it is what an approval is made against). Sign-out clears everything.

const SHELL = "thallo-shell-v1";
const READS = "thallo-reads-v1";
const PAGES = ["/review", "/plan", "/connections", "/login"];
const CACHED_API = ["/api/review/bundles", "/api/plan"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(PAGES)).catch(() => undefined));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== READS).map((k) => caches.delete(k)))),
  );
  self.clients.claim();
});

self.addEventListener("message", (event) => {
  if (event.data === "clear") event.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))));
});

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) (await caches.open(cacheName)).put(request, response.clone());
    return response;
  } catch {
    const hit = await caches.match(request);
    if (hit) return hit;
    throw new Error("offline");
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never intercept a change
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) {
    const exact = CACHED_API.some((p) => url.pathname === p);
    if (exact) event.respondWith(networkFirst(req, READS));
    return; // cards, media, session and connections: network only
  }
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || networkFirst(req, SHELL)));
    return;
  }
  if (req.mode === "navigate") event.respondWith(networkFirst(req, SHELL));
});
