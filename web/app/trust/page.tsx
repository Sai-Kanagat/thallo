"use client";

import { useState } from "react";

import { Empty, Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";

import "./trust.css";

type Task = {
  task_type: string;
  level: number;
  does_alone: string;
  paused: boolean;
  pause_reason: string | null;
  window_items: number;
  defects: number;
  escaped: number;
  next_level: number | null;
  can_raise: boolean;
  blocked_by: string[];
  bound_pct: string | null;
  canary_note: string | null;
};
type Trip = { id: string; scope: string; subject: string; cap_eur: string; spent_eur: string; day: string; reset: boolean };
type Overview = { tasks: Task[]; breakers: Trip[]; incidents: { id: string; severity: string; title: string }[]; can_raise: boolean };
type Sentence = { id: string; position: number; sentence: string; verdict: string; note: string | null };
type AuditWeek = { week_start: string; checked: number; wrong: number; note: string; sentences: Sentence[] };

function TaskRow({ t, canRaise, onDone }: { t: Task; canRaise: boolean; onDone: () => void }) {
  const [message, setMessage] = useState<string | null>(null);
  const raise = async () => {
    try {
      await post(`/api/trust/${encodeURIComponent(t.task_type)}/raise`);
      onDone();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  return (
    <li className="trust-task">
      <h3>
        {t.task_type.replaceAll("_", " ")}: level {t.level}
        {t.paused ? <span className="chip flag">Paused</span> : null}
      </h3>
      <p className="fine">{t.does_alone}</p>
      {t.pause_reason ? <p className="fine">{t.pause_reason}</p> : null}
      <p className="fine">
        {t.window_items} items at this level, {t.defects} defects, {t.escaped} escaped.
        {t.bound_pct ? ` The true defect rate could be up to ${t.bound_pct}% (95% bound).` : ""}
      </p>
      {t.canary_note ? <p className="fine">{t.canary_note}</p> : null}
      {t.next_level !== null && !t.can_raise ? (
        <ul className="fine">{t.blocked_by.map((r) => <li key={r}>{r}</li>)}</ul>
      ) : null}
      {canRaise && t.can_raise ? (
        <button className="button small primary" onClick={() => void raise()}>Raise to level {t.next_level}</button>
      ) : null}
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </li>
  );
}

function SentenceRow({ s, onDone }: { s: Sentence; onDone: () => void }) {
  const [message, setMessage] = useState<string | null>(null);
  const mark = async (verdict: "correct" | "wrong") => {
    try {
      await post(`/api/trust/audit/${s.id}`, { verdict });
      onDone();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  return (
    <li className="audit-sentence">
      <p>{s.position}. {s.sentence}</p>
      {s.verdict === "pending" ? (
        <div className="row">
          <button className="button small" onClick={() => void mark("correct")}>Correct</button>
          <button className="button small" onClick={() => void mark("wrong")}>Wrong</button>
        </div>
      ) : (
        <p className="quiet-text">{s.verdict === "wrong" ? "Marked wrong" : "Marked correct"}</p>
      )}
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </li>
  );
}

export default function TrustPage() {
  const [load, reload] = useLoad<Overview>("/api/trust");
  const [audit, reloadAudit] = useLoad<AuditWeek>("/api/trust/audit");
  if (load.state === "loading") return <Loading what="the trust ladder" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const o = load.data;
  const both = () => {
    reload();
    reloadAudit();
  };
  return (
    <section>
      <h1 className="page-title">How much the system does alone</h1>
      <p className="quiet-text">Levels rise only when you raise them, on measured results. Any slip drops them the same day.</p>
      {o.tasks.length === 0 ? (
        <Empty title="Nothing reviewed yet">Each kind of piece starts in shadow mode.</Empty>
      ) : (
        <ul className="trust-tasks">
          {o.tasks.map((t) => <TaskRow key={t.task_type} t={t} canRaise={o.can_raise} onDone={both} />)}
        </ul>
      )}

      <section aria-label="Cost breakers">
        <h2>Cost breakers this week</h2>
        {o.breakers.length === 0 ? <p className="fine">No breaker tripped.</p> : (
          <ul className="fine">
            {o.breakers.map((b) => (
              <li key={b.id}>
                {b.day}: {b.subject} {b.scope} cap EUR {b.cap_eur} reached (spent {b.spent_eur}){b.reset ? ", reset" : ""}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Weekly hand audit">
        <h2>This week&apos;s 20 sentences</h2>
        {audit.state === "loading" ? <Loading what="the sentences" /> : null}
        {audit.state === "error" ? <Failure error={audit.error} onRetry={reloadAudit} /> : null}
        {audit.state === "ready" ? (
          <>
            <p className="fine">{audit.data.note}</p>
            <ol className="audit-sentences">
              {audit.data.sentences.map((s) => <SentenceRow key={s.id} s={s} onDone={both} />)}
            </ol>
          </>
        ) : null}
      </section>
    </section>
  );
}
