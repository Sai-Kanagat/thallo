"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, get, getSession, post } from "@/lib/api";
import type { Membership, SessionView } from "@/lib/types";

type Step = "email" | "code" | "tenant";

export default function LoginPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSession()
      .then(async (s) => {
        if (!s.authenticated) return;
        const m = await get<{ memberships: Membership[] }>("/api/auth/tenants");
        setMemberships(m.memberships);
        setStep("tenant");
      })
      .catch(() => undefined);
  }, []);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const requestCode = () =>
    run(async () => {
      await getSession();
      await post("/api/auth/request-code", { email });
      setStep("code");
      setMessage("If that address can sign in, a code is on its way. It works for 10 minutes.");
    });

  const verify = () =>
    run(async () => {
      const s = await post<SessionView & { memberships: Membership[] }>("/api/auth/verify", { email, code });
      if (s.tenant) {
        router.replace("/review");
        return;
      }
      setMemberships(s.memberships);
      setStep("tenant");
    });

  const choose = (tenantId: string) =>
    run(async () => {
      await post("/api/auth/tenant", { tenant_id: tenantId });
      router.replace("/review");
    });

  return (
    <section className="login">
      <h1 className="display">Thallo</h1>
      <p className="tagline">Grown, not generated.</p>

      {step === "email" ? (
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void requestCode();
          }}
        >
          <label htmlFor="email">Your work email</label>
          <input id="email" type="email" autoComplete="email" inputMode="email" required value={email}
                 onChange={(e) => setEmail(e.target.value)} />
          <button type="submit" className="button primary big" disabled={busy || !email}>
            Send me a sign-in code
          </button>
          <p className="fine">No password. We send a one-time code to this address.</p>
        </form>
      ) : null}

      {step === "code" ? (
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void verify();
          }}
        >
          <label htmlFor="code">The 8-digit code</label>
          <input id="code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{8}" maxLength={8} required
                 value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
          <button type="submit" className="button primary big" disabled={busy || code.length !== 8}>
            Sign in
          </button>
          <button type="button" className="link-button" onClick={() => setStep("email")}>
            Use another address
          </button>
        </form>
      ) : null}

      {step === "tenant" ? (
        <div className="stack">
          <h2>Which business?</h2>
          {memberships.length === 0 ? <p>This address has no business to open yet. Ask your operator.</p> : null}
          <ul className="choices">
            {memberships.map((m) => (
              <li key={m.tenant_id}>
                <button type="button" className="choice" disabled={busy} onClick={() => void choose(m.tenant_id)}>
                  <span className="choice-name">{m.tenant_name}</span>
                  <span className="chip quiet">{m.role}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {message ? (
        <p className="note" role="status">
          {message}
        </p>
      ) : null}
    </section>
  );
}
