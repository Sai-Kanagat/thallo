// Empty every cache this origin holds, from the page and through the service worker. Called on
// sign-out and on tenant switch, so nothing of one session survives into the next.

export async function clearAllCaches(): Promise<void> {
  try {
    if (typeof caches !== "undefined") {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
    if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
      const reg = await navigator.serviceWorker.ready;
      reg.active?.postMessage("clear");
    }
  } catch {
    // nothing cached, or no service worker: nothing to clear
  }
}
