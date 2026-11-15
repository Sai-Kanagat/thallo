import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { clearAllCaches } from "./caches";

// A small fake of the service worker globals, enough to run public/sw.js and watch every write.
class FakeCaches {
  stores = new Map<string, Map<string, Response>>();
  async open(name: string) {
    if (!this.stores.has(name)) this.stores.set(name, new Map());
    const store = this.stores.get(name)!;
    return {
      put: async (req: Request | string, res: Response) => void store.set(typeof req === "string" ? req : req.url, res),
      addAll: async (urls: string[]) => urls.forEach((u) => store.set(new URL(u, ORIGIN).href, new Response("page"))),
    };
  }
  async match(req: Request) {
    for (const s of this.stores.values()) if (s.has(req.url)) return s.get(req.url);
    return undefined;
  }
  async keys() {
    return [...this.stores.keys()];
  }
  async delete(name: string) {
    return this.stores.delete(name);
  }
  urls() {
    return [...this.stores.values()].flatMap((s) => [...s.keys()]);
  }
}

const ORIGIN = "https://app.thallo.test";

function loadWorker(online = true) {
  const handlers: Record<string, (e: unknown) => void> = {};
  const caches = new FakeCaches();
  const self = {
    location: new URL(ORIGIN),
    addEventListener: (name: string, fn: (e: unknown) => void) => (handlers[name] = fn),
    skipWaiting: () => undefined,
    clients: { claim: () => undefined },
  };
  const fetchImpl = vi.fn(async () => {
    if (!online) throw new TypeError("offline");
    return new Response(JSON.stringify({ tenant: "A" }), { status: 200 });
  });
  const code = readFileSync(join(__dirname, "../public/sw.js"), "utf8");
  new Function("self", "caches", "fetch", code)(self, caches, fetchImpl);
  return { handlers, caches, fetchImpl };
}

async function dispatchFetch(h: Record<string, (e: unknown) => void>, path: string, init: RequestInit & { mode?: string } = {}) {
  const request = new Request(`${ORIGIN}${path}`, { method: init.method ?? "GET" });
  Object.defineProperty(request, "mode", { value: init.mode ?? "cors" });
  let responded: Promise<Response> | null = null;
  h.fetch!({ request, respondWith: (p: Promise<Response>) => (responded = p) });
  if (responded) await (responded as Promise<Response>).catch(() => undefined);
  return responded !== null;
}

describe("service worker", () => {
  it("never writes or serves an /api/ response, signed in or not", async () => {
    const { handlers, caches } = loadWorker();
    for (const path of ["/api/review/bundles", "/api/plan", "/api/plan?week=2026-01-05", "/api/review/bundles/b1",
                        "/api/media/m1", "/api/session", "/api/connections"]) {
      expect(await dispatchFetch(handlers, path)).toBe(false); // passed straight to the network
    }
    await dispatchFetch(handlers, "/review", { mode: "navigate" });
    await dispatchFetch(handlers, "/_next/static/chunk.js");
    expect(caches.urls().some((u) => new URL(u).pathname.startsWith("/api/"))).toBe(false);
    expect(caches.urls()).toContain(`${ORIGIN}/review`);
  });

  it("never intercepts a change, online or offline", async () => {
    const { handlers } = loadWorker(false);
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      expect(await dispatchFetch(handlers, "/api/review/bundles/b1/approve", { method })).toBe(false);
    }
  });

  it("empties every cache on the clear message", async () => {
    const { handlers, caches } = loadWorker();
    await dispatchFetch(handlers, "/plan", { mode: "navigate" });
    expect(caches.urls().length).toBeGreaterThan(0);
    let wait: Promise<unknown> = Promise.resolve();
    handlers.message!({ data: "clear", waitUntil: (p: Promise<unknown>) => (wait = p) });
    await wait;
    expect(await caches.keys()).toEqual([]);
  });
});

describe("clearAllCaches on sign-out and tenant switch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("deletes every cache from the page and tells the worker", async () => {
    const fake = new FakeCaches();
    await (await fake.open("thallo-shell-v2")).put(`${ORIGIN}/review`, new Response("x"));
    await fake.open("anything-else");
    const postMessage = vi.fn();
    vi.stubGlobal("caches", fake);
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { ready: Promise.resolve({ active: { postMessage } }) },
    });
    await clearAllCaches();
    expect(await fake.keys()).toEqual([]);
    expect(postMessage).toHaveBeenCalledWith("clear");
  });
});
