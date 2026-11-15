"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";

// Designed states for every screen: loading, empty, error (with the next step) and offline.

export function Loading({ what }: { what: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="spore" aria-hidden="true" />
      <span className="spore" aria-hidden="true" />
      <span className="spore" aria-hidden="true" />
      <p>Loading {what}</p>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <svg className="state-mark" viewBox="0 0 48 48" aria-hidden="true">
        <path d="M24 42c0-10 0-16 0-24M24 26c-6-1-10-5-11-11 6 1 10 5 11 11zM24 20c5-1 9-4 10-10-5 1-9 4-10 10z" />
      </svg>
      <h2>{title}</h2>
      {children ? <div className="state-body">{children}</div> : null}
    </div>
  );
}

export function Failure({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  if (error.code === "offline") return <Offline onRetry={onRetry} />;
  if (error.status === 401) {
    return (
      <div className="state state-error" role="alert">
        <h2>You are signed out</h2>
        <p>Sessions end after 15 minutes without activity.</p>
        <Link className="button primary" href="/login">
          Sign in again
        </Link>
      </div>
    );
  }
  if (error.status === 403) {
    return (
      <div className="state state-error" role="alert">
        <h2>Not for your role</h2>
        <p>{error.message}. Ask the founder or the operator if you need this screen.</p>
      </div>
    );
  }
  return (
    <div className="state state-error" role="alert">
      <h2>That did not load</h2>
      <p>{error.message}</p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function Offline({ onRetry }: { onRetry?: () => void }) {
  return (
    <div className="state state-offline" role="alert">
      <h2>You are offline</h2>
      <p>This screen needs a connection to show current data. Nothing you tap is saved or queued while offline.</p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}
