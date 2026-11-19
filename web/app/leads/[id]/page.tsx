"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";

import styles from "../leads.module.css";
import type { LeadDetail, LeadMessage, LeadQuestion, SendOutcome } from "../types";
import { CONSENT, LOSS_REASONS, ROUTE, clockText, safeAppLink } from "../words";

const KIND: Record<string, string> = { acknowledgement: "Acknowledgement (approved wording)", reply_draft: "Drafted reply" };

export default function LeadPage() {
  const { id } = useParams<{ id: string }>();
  const [load, reload] = useLoad<LeadDetail>(`/api/leads/${id}`);
  const [note, setNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const [opened, setOpened] = useState<Record<string, string>>({});

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setNote(null);
    try {
      await fn();
      setNote({ text: done });
      reload();
    } catch (e) {
      setNote({ text: e instanceof ApiError ? e.message : "That did not save.", bad: true });
    }
  };

  if (load.state === "loading") return <Loading what="the enquiry" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const lead = load.data;

  const send = (m: LeadMessage) =>
    act(async () => {
      const out = await post<SendOutcome>(`/api/leads/messages/${m.id}/send`, { body_sha256: m.body_sha256 });
      const link = safeAppLink(out.link);
      if (out.status === "open_in_app" && link) setOpened((o) => ({ ...o, [m.id]: link }));
      if (out.status === "failed") throw new ApiError(0, "failed", out.detail ?? "The send could not be confirmed.");
    }, "Approved. Open it in your app and send it yourself.");

  return (
    <section className={styles.detail}>
      <div className={styles.section}>
        <Link href="/leads" className="back">All enquiries</Link>
        <h1 className="page-title">{lead.name}</h1>
        <p className="card-meta">
          {lead.person.phone ? <span>{lead.person.phone}</span> : null}
          {lead.person.email ? <span>{lead.person.email}</span> : null}
          <span>{lead.state.replace("_", " ")}</span>
          {lead.route ? <span>{ROUTE[lead.route]}</span> : null}
        </p>
        <div className={styles.two}>
          <span><b>{lead.fit}</b>fit of 100</span>
          <span><b>{Math.round(lead.heat)}</b>heat, halves in 14 days</span>
        </div>
        <p className={lead.clock.overdue ? "note bad" : "note"} role="status">{clockText(lead.clock)}</p>
        {!lead.may_message ? (
          <p className="note bad">{CONSENT[lead.consent_reason ?? ""] ?? "No consent basis: reply personally"}</p>
        ) : null}
        {lead.unverified_contacts.length > 0 ? (
          <p className="fine">
            Also given, not verified: {lead.unverified_contacts.map((c) => c.value).join(", ")}. Nothing is sent there.
          </p>
        ) : null}
        {note ? <p className={note.bad ? "note bad" : "note"} role="status">{note.text}</p> : null}

        <h2>Messages</h2>
        {lead.messages.length === 0 ? <p className="quiet-text">Nothing drafted.</p> : null}
        {lead.messages.map((m) => (
          <Message key={m.id} m={m} link={opened[m.id]} onSend={() => send(m)}
                   onSent={() => act(() => post(`/api/leads/messages/${m.id}/sent`), "Marked as sent.")}
                   onDiscard={() => act(() => post(`/api/leads/messages/${m.id}/discard`), "Discarded.")}
                   onEdit={(body) => act(() => post(`/api/leads/messages/${m.id}/edit`, { body }), "Saved. Read it once more before sending.")} />
        ))}
      </div>

      <div className={styles.section}>
        <h2>Questions</h2>
        <Questions questions={lead.questions}
                   onSave={(answers) => act(() => post(`/api/leads/${lead.id}/answers`, { answers }), "Answers saved.")} />
        <h2>Why this fit</h2>
        <table className={styles.table}>
          <tbody>
            {lead.fit_lines.map((l) => (
              <tr key={l.rule}><td>{l.rule}</td><td>{l.points > 0 ? `+${l.points}` : l.points}</td></tr>
            ))}
          </tbody>
        </table>
        <h2>Outcome</h2>
        <Outcome onAct={(path, body, done) => act(() => post(`/api/leads/${lead.id}/${path}`, body), done)} />
      </div>
    </section>
  );
}

function Message({ m, link, onSend, onSent, onDiscard, onEdit }: {
  m: LeadMessage; link?: string; onSend: () => void; onSent: () => void; onDiscard: () => void;
  onEdit: (body: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.body);
  const draft = m.state === "drafted";
  return (
    <article className={styles.message}>
      <p className="card-meta"><span>{KIND[m.kind] ?? m.kind}</span><span>{m.channel}</span><span>{m.send_state ?? m.state}</span></p>
      {editing ? (
        <>
          <label className="sr-only" htmlFor={`b-${m.id}`}>Message text</label>
          <textarea id={`b-${m.id}`} className={styles.textarea} value={text} maxLength={2000}
                    onChange={(e) => setText(e.target.value)} />
          <div className="row">
            <button type="button" className="button small" onClick={() => { onEdit(text); setEditing(false); }}>Save text</button>
            <button type="button" className="link-button" onClick={() => { setText(m.body); setEditing(false); }}>Cancel</button>
          </div>
        </>
      ) : (
        <p className={styles.body}>{m.body}</p>
      )}
      {draft && !editing ? (
        <div className="row">
          {m.send_state === null ? (
            <button type="button" className="button" onClick={onSend}>Approve this message</button>
          ) : null}
          {link ? <a className="button" href={link} target="_blank" rel="noopener noreferrer">Open in my app</a> : null}
          {m.send_state === "opened" ? (
            <button type="button" className="button small" onClick={onSent}>I sent it</button>
          ) : null}
          {m.editable ? <button type="button" className="button small" onClick={() => setEditing(true)}>Edit</button> : null}
          {m.send_state === null ? (
            <button type="button" className="button small danger" onClick={onDiscard}>Discard</button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function Questions({ questions, onSave }: { questions: LeadQuestion[]; onSave: (a: Record<string, string>) => void }) {
  const [vals, setVals] = useState<Record<string, string>>({});
  const set = (k: string, v: string) => setVals((x) => ({ ...x, [k]: v }));
  return (
    <form className={styles.section} onSubmit={(e) => { e.preventDefault(); onSave(vals); }}>
      {questions.map((q) => (
        <div key={q.key} className={styles.question}>
          <label htmlFor={`q-${q.key}`}>{q.ask}</label>
          {q.type === "choice" ? (
            <select id={`q-${q.key}`} className={styles.input} value={vals[q.key] ?? q.answer ?? ""}
                    onChange={(e) => set(q.key, e.target.value)}>
              <option value="" disabled>Choose</option>
              {q.options.map((o) => <option key={o} value={o}>{o.replaceAll("_", " ")}</option>)}
            </select>
          ) : (
            <input id={`q-${q.key}`} className={styles.input} maxLength={200} value={vals[q.key] ?? q.answer ?? ""}
                   onChange={(e) => set(q.key, e.target.value)} />
          )}
        </div>
      ))}
      <button type="submit" className="button" disabled={Object.keys(vals).length === 0}>Save answers</button>
    </form>
  );
}

function Outcome({ onAct }: { onAct: (path: string, body: unknown, done: string) => void }) {
  const [when, setWhen] = useState("");
  const [kind, setKind] = useState("Visit");
  const [amount, setAmount] = useState("");
  return (
    <div className={styles.section}>
      <div className={styles.question}>
        <label htmlFor="book-when">Booked for</label>
        <input id="book-when" type="datetime-local" className={styles.input} value={when} onChange={(e) => setWhen(e.target.value)} />
        <label htmlFor="book-kind">What</label>
        <input id="book-kind" className={styles.input} maxLength={40} value={kind} onChange={(e) => setKind(e.target.value)} />
        <button type="button" className="button" disabled={!when}
                onClick={() => onAct("book", { starts_at: new Date(when).toISOString(), kind }, "Booking saved.")}>
          Save booking
        </button>
      </div>
      <div className="row">
        <button type="button" className="button small" onClick={() => onAct("outcome", { kind: "held" }, "Marked held.")}>Held</button>
        <button type="button" className="button small" onClick={() => onAct("outcome", { kind: "no_show" }, "Marked no-show.")}>No-show</button>
      </div>
      <div className={styles.question}>
        <label htmlFor="won-amount">Won, amount</label>
        <input id="won-amount" inputMode="decimal" className={styles.input} value={amount}
               onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} />
        <button type="button" className="button" disabled={!amount}
                onClick={() => onAct("outcome", { kind: "won", amount }, "Marked won.")}>Mark won</button>
      </div>
      <p className="fine">Lost, because</p>
      <div className="reasons">
        {LOSS_REASONS.map(([value, label]) => (
          <button key={value} type="button" className="button small"
                  onClick={() => onAct("outcome", { kind: "lost", reason: value }, "Marked lost.")}>{label}</button>
        ))}
      </div>
    </div>
  );
}
