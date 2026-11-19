// Offline shell: the app's pages and static assets only. No /api/ response is ever cached or
// served from cache, so one tenant's data (or a signed-out person's) can never appear after a
// tenant switch, a sign-out or on a shared phone; /api/ goes straight to the network and the
// app shows its "you are offline" state instead of stale data. Only GET is handled at all: every
// change goes to the network, so nothing made offline is queued or replayed. The app clears every
// cache on sign-out and on tenant switch (lib/caches.ts).

const SHELL = "thallo-shell-v2";
const PAGES = ["/review", "/plan", "/growth", "/connections", "/login"];

function cacheable(url) {
  return url.origin === self.location.origin && !url.pathname.startsWith("/api/");
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(PAGES)).catch(() => undefined));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("message", (event) => {
  if (event.data === "clear") event.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))));
});

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok && cacheable(new URL(request.url))) (await caches.open(SHELL)).put(request, response.clone());
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
  if (!cacheable(url)) return; // /api/ and other origins: the network, untouched
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || networkFirst(req)));
    return;
  }
  if (req.mode === "navigate") event.respondWith(networkFirst(req));
});
