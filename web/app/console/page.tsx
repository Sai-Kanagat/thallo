"use client";

import { useState } from "react";

import { Empty, Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";

import "./console.css";

type Cost = {
  today_eur: string;
  month_eur: string;
  day_cap_eur: string | null;
  day_pct: number | null;
  cap_note: string | null;
  providers: { provider: string; spent_eur: string; cap_eur: string | null; pct: number | null }[];
};
type Queue = { status: string; counts: Record<string, number>; due_waiting: number; stuck_running: number;
  oldest_due_minutes: number | null };
type Client = {
  tenant_id: string;
  name: string;
  operator: string;
  paused: boolean;
  open_incidents: number;
  queue: Queue;
  cost: Cost;
  breakers: { id: string; subject: string; scope: string; day: string; cap_eur: string; spent_eur: string;
    reset: boolean }[];
  prompts: { name: string; version: number; role: string; model_id: string; eval_passed: boolean }[];
  evals: { change_kind: string; change_name: string; passed: boolean; cases: number; failed: number;
    ran_at: string }[];
  expiries: { kind: string; label: string; days_left: number; expired: boolean }[];
};
type Overview = { generated_at: string; tenants: Client[] };
type Token = { id: string; name: string; prefix: string; scopes: string[]; expires_at: string; revoked: boolean;
  expired: boolean; last_used_at: string | null };
type Tokens = { tokens: Token[]; scopes: string[]; max_days: number };

const QUEUE_WORDS: Record<string, string> = { ok: "Running on time", behind: "Behind", attention: "Needs a look" };

function ClientCard({ c }: { c: Client }) {
  const cost = c.cost;
  return (
    <li className="console-client">
      <h2>
        {c.name}
        {c.paused ? <span className="chip flag">Paused</span> : null}
        {c.open_incidents ? <span className="chip flag">{c.open_incidents} open incident(s)</span> : null}
      </h2>
      <p className="fine">
        Queue: {QUEUE_WORDS[c.queue.status] ?? c.queue.status}. {c.queue.due_waiting} due,{" "}
        {c.queue.stuck_running} stuck, {c.queue.counts.dead ?? 0} dead.
        {c.queue.oldest_due_minutes !== null ? ` Oldest waiting ${c.queue.oldest_due_minutes} min.` : ""}
      </p>
      <p className="fine">
        Cost today EUR {cost.today_eur}
        {cost.day_cap_eur !== null ? ` of EUR ${cost.day_cap_eur} (${cost.day_pct}%)` : `, ${cost.cap_note}`}.
        This month EUR {cost.month_eur}.
      </p>
      {c.breakers.length ? (
        <ul className="fine">
          {c.breakers.map((b) => (
            <li key={b.id}>{b.day}: {b.subject} {b.scope} breaker tripped at EUR {b.cap_eur}{b.reset ? ", reset" : ""}</li>
          ))}
        </ul>
      ) : <p className="fine">No breaker tripped this week.</p>}
      <p className="fine">
        Prompts: {c.prompts.length ? c.prompts.map((p) => `${p.name} v${p.version}${p.eval_passed ? "" : " (eval not passed)"}`)
          .join(", ") : "none active"}.
      </p>
      {c.evals.length ? (
        <ul className="fine">
          {c.evals.map((e) => (
            <li key={`${e.change_name}-${e.ran_at}`}>
              {e.change_kind} {e.change_name}: {e.passed ? "passed" : `failed ${e.failed} of ${e.cases}`}
            </li>
          ))}
        </ul>
      ) : null}
      {c.expiries.length ? (
        <ul className="fine">
          {c.expiries.map((x) => (
            <li key={x.label}>{x.label}: {x.expired ? "expired" : `expires in ${x.days_left} days`}</li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function TokenSection() {
  const [load, reload] = useLoad<Tokens>("/api/tokens");
  const [name, setName] = useState("");
  const [draft, setDraft] = useState(false);
  const [days, setDays] = useState(30);
  const [fresh, setFresh] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  if (load.state === "loading") return <Loading what="API tokens" />;
  if (load.state === "error") {
    return load.error instanceof ApiError && load.error.status === 403 ? null : <Failure error={load.error} onRetry={reload} />;
  }
  const create = async () => {
    try {
      const r = await post<{ token: string }>("/api/tokens", { name, scopes: draft ? ["read", "draft"] : ["read"], days });
      setFresh(r.token);
      setName("");
      setMessage(null);
      reload();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  const revoke = async (id: string) => {
    try {
      await post(`/api/tokens/${id}/revoke`);
      reload();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  return (
    <section aria-label="API tokens">
      <h2>API tokens for this business</h2>
      <p className="fine">A token reads, and can draft if you allow it. It can never approve, send, publish or spend.</p>
      {fresh ? (
        <p className="console-fresh" role="status">Copy this token now. It is not shown again: <code>{fresh}</code></p>
      ) : null}
      <div className="console-form">
        <label>Name <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></label>
        <label><input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} /> Can draft</label>
        <label>Days
          <input type="number" min={1} max={load.data.max_days} value={days} onChange={(e) => setDays(Number(e.target.value))} />
        </label>
        <button className="button small primary" disabled={!name.trim()} onClick={() => void create()}>Make token</button>
      </div>
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
      {load.data.tokens.length === 0 ? <p className="fine">No tokens yet.</p> : (
        <ul className="console-tokens">
          {load.data.tokens.map((t) => (
            <li key={t.id}>
              <span>{t.name} ({t.prefix}...) {t.scopes.join(", ")}</span>
              <span className="fine">
                {t.revoked ? "revoked" : t.expired ? "expired" : `until ${t.expires_at.slice(0, 10)}`}
                {t.last_used_at ? `, last used ${t.last_used_at.slice(0, 10)}` : ", never used"}
              </span>
              {!t.revoked && !t.expired ? (
                <button className="button small" onClick={() => void revoke(t.id)}>Revoke</button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ConsolePage() {
  const [load, reload] = useLoad<Overview>("/api/console");
  let clients;
  if (load.state === "loading") clients = <Loading what="your clients" />;
  else if (load.state === "error") {
    clients = load.error instanceof ApiError && load.error.status === 403
      ? <Empty title="No operator on this sign-in">The console shows the clients of an operator you run.</Empty>
      : <Failure error={load.error} onRetry={reload} />;
  } else if (load.data.tenants.length === 0) clients = <Empty title="No clients yet">Clients appear here once added.</Empty>;
  else clients = <ul className="console-clients">{load.data.tenants.map((c) => <ClientCard key={c.tenant_id} c={c} />)}</ul>;
  return (
    <section>
      <h1 className="page-title">All your clients</h1>
      <p className="quiet-text">Queue, cost, breakers, prompts, evals and expiring logins, one row per client.</p>
      {clients}
      <TokenSection />
    </section>
  );
}
