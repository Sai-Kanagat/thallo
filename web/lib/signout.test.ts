import { afterEach, describe, expect, it, vi } from "vitest";

import { setCsrf } from "./api";
import { signOut } from "./signout";

function setWorker(value: unknown) {
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setWorker(undefined);
});

describe("sign-out", () => {
  it("completes with no service worker registered", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: "signed_out" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setWorker({
      ready: new Promise(() => undefined), // what hangs forever with no worker
      getRegistration: async () => undefined,
    });
    setCsrf("token");
    const redirect = vi.fn();
    await signOut(redirect);
    expect(redirect).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/logout", expect.objectContaining({ method: "POST" }));
  });

  it("still redirects when offline and when cache clearing never answers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("offline");
    }));
    vi.stubGlobal("caches", { keys: () => new Promise(() => undefined), delete: async () => true });
    setWorker({ getRegistration: () => new Promise(() => undefined) });
    setCsrf("token");
    const redirect = vi.fn();
    vi.useFakeTimers();
    const done = signOut(redirect);
    await vi.advanceTimersByTimeAsync(600);
    await done;
    vi.useRealTimers();
    expect(redirect).toHaveBeenCalledTimes(1);
  });

  it("tells a registered worker to clear and deletes page caches", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    const deleted: string[] = [];
    vi.stubGlobal("caches", { keys: async () => ["thallo-shell-v2"], delete: async (k: string) => deleted.push(k) });
    const postMessage = vi.fn();
    setWorker({ getRegistration: async () => ({ active: { postMessage } }) });
    setCsrf("token");
    await signOut(() => undefined);
    expect(deleted).toEqual(["thallo-shell-v2"]);
    expect(postMessage).toHaveBeenCalledWith("clear");
  });
});
