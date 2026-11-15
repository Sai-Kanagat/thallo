// The only way the app talks to the API: same origin, cookie session, CSRF header on every
// state-changing call. Nothing is ever queued or retried offline: an approval made offline fails
// and says so, and the person approves again when back online.

import type { SessionView } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

let csrf: string | null = null;

export function setCsrf(token: string | null) {
  csrf = token;
}

const OFFLINE_MESSAGE = "You are offline. Nothing was sent. Try again when you are back online.";

async function send(path: string, init: RequestInit): Promise<Response> {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    throw new ApiError(0, "offline", OFFLINE_MESSAGE);
  }
  try {
    return await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  } catch {
    throw new ApiError(0, "offline", OFFLINE_MESSAGE);
  }
}

async function read<T>(r: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await r.json();
  } catch {
    body = null;
  }
  if (!r.ok) {
    const b = (body ?? {}) as { error?: string; message?: string };
    throw new ApiError(r.status, b.error ?? "error", b.message ?? "Something went wrong. Reload the page.");
  }
  return body as T;
}

export async function getSession(): Promise<SessionView> {
  const s = await read<SessionView>(await send("/api/session", { method: "GET" }));
  setCsrf(s.csrf_token);
  return s;
}

export async function get<T>(path: string): Promise<T> {
  return read<T>(await send(path, { method: "GET" }));
}

export async function post<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  if (csrf === null) await getSession();
  const r = await send(path, {
    method,
    headers: { "content-type": "application/json", "x-csrf-token": csrf ?? "" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const out = await read<T & { csrf_token?: string }>(r);
  if (out && typeof out === "object" && typeof out.csrf_token === "string") setCsrf(out.csrf_token);
  return out;
}

export function shortHash(h: string | null): string {
  return h ? `${h.slice(0, 8)} ${h.slice(8, 16)}` : "none";
}
