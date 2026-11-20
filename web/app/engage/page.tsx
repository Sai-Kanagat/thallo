"use client";

import { useState } from "react";

import { Empty, Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";

import styles from "./engage.module.css";

type Item = {
  id: string;
  channel: string;
  source: "comment" | "message";
  author: string | null;
  text: string;
  permalink: string | null;
  received_at: string;
  category: string;
  reasons: string[];
  state: string;
  flagged: boolean;
  acknowledged: boolean;
  suggested_reply: string | null;
  suggested_by_model: boolean;
  lead_id: string | null;
};

type Act = (path: string, body?: unknown, done?: string) => void;

const CATEGORY: Record<string, string> = {
  question: "Question", praise: "Kind words", complaint: "Complaint", lead: "Possible enquiry",
  abuse: "Abusive", crisis: "Safety or legal risk", spam: "Spam", other: "Other",
};

const where = (c: string) => c.replace(/_/g, " ");

function Card({ item, onAct }: { item: Item; onAct: Act }) {
  const [reply, setReply] = useState(item.suggested_reply ?? "");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(reply);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <li className={styles.item}>
      <div className={styles.head}>
        <span className={styles.who}>{item.author ?? "Someone"}</span>
        <span className={styles.where}>{item.source} on {where(item.channel)}</span>
      </div>
      <div className={styles.meta}>
        <span className={item.flagged ? "chip blocked" : item.category === "lead" ? "chip info" : "chip"}>
          {CATEGORY[item.category] ?? item.category}
        </span>
        {item.reasons.map((r) => <span key={r} className={styles.reason}>{r}</span>)}
      </div>
      <blockquote className={styles.quote}>{item.text}</blockquote>
      {item.state === "flagged" ? (
        <div className={styles.flag} role="alert">
          <p>Flagged for a person first. No reply is suggested until you have read it. Think twice before replying in public, and tell your operator if it names a safety or legal risk.</p>
          <div className={styles.actions}>
            <button className="button primary" onClick={() => onAct(`${item.id}/acknowledge`, undefined, "Marked as read.")}>I have read it</button>
            <button className="button quiet" onClick={() => onAct(`${item.id}/dismiss`, undefined, "Dismissed.")}>Dismiss</button>
          </div>
        </div>
      ) : (
        <div className={styles.reply}>
          <label htmlFor={`r-${item.id}`}>Suggested reply{item.suggested_by_model ? ", drafted by a model" : ""}</label>
          <textarea id={`r-${item.id}`} className={styles.input} rows={3} value={reply}
                    onChange={(e) => { setReply(e.target.value); setCopied(false); }}
                    placeholder="No suggestion. Write your own reply in the platform." />
          <p className={styles.note}>Thallo never posts this. Copy it, reply in the platform yourself, then mark it done.</p>
          <div className={styles.actions}>
            <button className="button primary" disabled={!reply.trim()} onClick={copy}>{copied ? "Copied" : "Copy reply"}</button>
            {item.permalink ? (
              <a className="button" href={item.permalink} target="_blank" rel="noopener noreferrer">Open in {where(item.channel)}</a>
            ) : null}
            {reply.trim() && reply !== item.suggested_reply ? (
              <button className="button" onClick={() => onAct(`${item.id}/edit`, { reply }, "Saved.")}>Save edit</button>
            ) : null}
            <button className="button" onClick={() => onAct(`${item.id}/handled`, undefined, "Marked as replied.")}>I replied</button>
            {item.category === "lead" || item.source === "message" ? (
              <button className="button" onClick={() => onAct(`${item.id}/lead`, {}, "Moved to the lead desk.")}>Move to leads</button>
            ) : null}
            <button className="button quiet" onClick={() => onAct(`${item.id}/dismiss`, undefined, "Dismissed.")}>Dismiss</button>
          </div>
        </div>
      )}
    </li>
  );
}

export default function EngagePage() {
  const [load, reload] = useLoad<{ items: Item[] }>("/api/engage");
  const [note, setNote] = useState<string | null>(null);
  if (load.state === "loading") return <Loading what="the inbox" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const act: Act = async (path, body, done) => {
    try {
      await post(`/api/engage/${path}`, body);
      setNote(done ?? "Done.");
      reload();
    } catch (e) {
      setNote(e instanceof ApiError ? e.message : "That did not work. Try again.");
    }
  };
  const items = load.data.items;
  return (
    <section>
      <h1 className="page-title">Inbox</h1>
      <p className="page-sub">Comments and messages, with a reply you can copy. You send it, in the platform.</p>
      {note ? <p className={styles.status} role="status">{note}</p> : null}
      {items.length === 0 ? (
        <Empty title="Nothing waiting">
          <p>New comments and messages land here. Flagged ones come first.</p>
        </Empty>
      ) : (
        <ul className={styles.list}>
          {items.map((i) => <Card key={`${i.id}:${i.state}:${i.suggested_reply ?? ""}`} item={i} onAct={act} />)}
        </ul>
      )}
    </section>
  );
}
