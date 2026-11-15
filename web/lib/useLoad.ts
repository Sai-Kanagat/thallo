"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, get } from "./api";

export type Load<T> =
  | { state: "loading" }
  | { state: "ready"; data: T }
  | { state: "error"; error: ApiError };

export function useLoad<T>(path: string | null): [Load<T>, () => void] {
  const [load, setLoad] = useState<Load<T>>({ state: "loading" });
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (path === null) return;
    let live = true;
    get<T>(path)
      .then((data) => live && setLoad({ state: "ready", data }))
      .catch((e: unknown) => {
        const error = e instanceof ApiError ? e : new ApiError(0, "error", "Something went wrong. Reload the page.");
        if (live) setLoad({ state: "error", error });
      });
    return () => {
      live = false;
    };
  }, [path, tick]);

  return [load, reload];
}
