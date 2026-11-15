// Empty every cache this origin holds, best effort, never blocking. Called alongside sign-out and
// tenant switch: the page deletes every cache itself, and tells the worker too if one is
// registered (getRegistration, never `ready`, which waits forever when there is no worker).

export async function clearAllCaches(): Promise<void> {
  const jobs: Promise<unknown>[] = [];
  try {
    if (typeof caches !== "undefined") {
      jobs.push(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))));
    }
    if (typeof navigator !== "undefined" && navigator.serviceWorker?.getRegistration) {
      jobs.push(navigator.serviceWorker.getRegistration().then((reg) => reg?.active?.postMessage("clear")));
    }
  } catch {
    return;
  }
  await Promise.allSettled(jobs);
}

/** Never lets cache clearing hold up what must happen (revoke, redirect): at most `ms` of waiting. */
export function clearAllCachesSoon(ms = 500): Promise<void> {
  return Promise.race([clearAllCaches(), new Promise<void>((resolve) => setTimeout(resolve, ms))]);
}
