"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { getSession, post } from "@/lib/api";
import type { SessionView } from "@/lib/types";

const NAV = [
  { href: "/review", label: "Review" },
  { href: "/plan", label: "Week" },
  { href: "/connections", label: "Connections" },
];

function useOnline() {
  const [online, setOnline] = useState(true);
  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  return online;
}

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const online = useOnline();
  const [session, setSession] = useState<SessionView | null>(null);

  useEffect(() => {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  }, []);

  useEffect(() => {
    getSession()
      .then((s) => {
        setSession(s);
        if (!s.authenticated && path !== "/login") router.replace("/login");
      })
      .catch(() => undefined); // offline: keep showing cached views
  }, [path, router]);

  const signOut = async () => {
    try {
      await post("/api/auth/logout");
    } finally {
      navigator.serviceWorker?.controller?.postMessage("clear");
      router.replace("/login");
    }
  };

  const signedIn = session?.authenticated && session.tenant;
  return (
    <div className="shell">
      <header className="topbar">
        <Link href="/review" className="brand" aria-label="Thallo, review deck">
          <svg viewBox="0 0 24 24" aria-hidden="true" className="brand-mark">
            <path d="M12 21V11M12 14c-3.5-.5-6-3-6.5-6.5 3.5.5 6 3 6.5 6.5zM12 11c3-.5 5-2.5 5.5-5.5-3 .5-5 2.5-5.5 5.5z" />
          </svg>
          <span>Thallo</span>
        </Link>
        {signedIn ? (
          <div className="who">
            <Link href="/login?switch=1" className="tenant" title="Switch business">
              {session.tenant?.tenant_name}
            </Link>
            <button type="button" className="link-button" onClick={signOut}>
              Sign out
            </button>
          </div>
        ) : null}
      </header>
      {!online ? (
        <p className="offline-bar" role="status">
          Offline. You are seeing the last saved view. Nothing you tap is sent until you are back.
        </p>
      ) : null}
      <main className="main">{children}</main>
      {signedIn ? (
        <nav className="tabs" aria-label="Screens">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} className={path.startsWith(n.href) ? "tab active" : "tab"}
                  aria-current={path.startsWith(n.href) ? "page" : undefined}>
              {n.label}
            </Link>
          ))}
        </nav>
      ) : null}
    </div>
  );
}
