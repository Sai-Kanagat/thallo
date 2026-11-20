"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { Mark } from "@/components/Mark";
import { getSession } from "@/lib/api";
import { signOut } from "@/lib/signout";
import type { SessionView } from "@/lib/types";

const NAV = [
  { href: "/review", label: "Review" },
  { href: "/plan", label: "Week" },
  { href: "/growth", label: "Growth" },
  { href: "/voice", label: "Voice" },
  { href: "/leads", label: "Leads" },
  { href: "/trust", label: "Trust" },
  { href: "/pipeline", label: "Pipeline" },
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

  const onSignOut = () => void signOut(() => router.replace("/login"));

  const signedIn = session?.authenticated && session.tenant;
  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link href="/review" className="brand" aria-label="Thallo, review deck">
          <Mark />
          <span>Thallo</span>
        </Link>
        {signedIn ? (
          <>
            <nav className="tabs" aria-label="Screens">
              {NAV.map((n) => (
                <Link key={n.href} href={n.href} className={path.startsWith(n.href) ? "tab active" : "tab"}
                      aria-current={path.startsWith(n.href) ? "page" : undefined}>
                  {n.label}
                </Link>
              ))}
            </nav>
            <div className="who">
              <Link href="/login?switch=1" className="tenant" title="Switch business">
                <span>{session.tenant?.tenant_name}</span>
              </Link>
              <button type="button" className="button quiet small" onClick={onSignOut}>
                Sign out
              </button>
            </div>
          </>
        ) : null}
      </header>
      {!online ? (
        <p className="offline-bar" role="status">
          You are offline. Nothing loads or sends until the connection is back.
        </p>
      ) : null}
      <main id="main" className="main">
        {children}
      </main>
    </div>
  );
}
