"use client";

import { useEffect, useRef, useState } from "react";

import { shortHash } from "@/lib/api";
import { type Blocking, type Card, REJECTION_REASONS } from "@/lib/types";

// One card per bundle. Everything shown is what the server hashed; the client sends back only
// the flag ids it acknowledged and the seconds this card was visible. All text is untrusted and
// rendered as text.

const NEXT_STEP: Record<Blocking["kind"], string> = {
  fail: "Edit the text; the checks run again on the new version.",
  not_checked: "Wait for the checks to run on this version, then reopen the card.",
  problem: "Ask the operator to look at this bundle.",
};

export type ReviewCardProps = {
  card: Card;
  busy?: boolean;
  onApprove: (acknowledgedFlagIds: string[], secondsOnScreen: number) => void;
  onEdit: (variantId: string, text: string) => void;
  onReject: (reason: string) => void;
  onLater?: () => void;
  now?: () => number;
};

function useVisibleSeconds(now: () => number) {
  const total = useRef(0);
  const since = useRef<number | null>(null);
  useEffect(() => {
    const start = () => {
      if (since.current === null) since.current = now();
    };
    const stop = () => {
      if (since.current !== null) total.current += now() - since.current;
      since.current = null;
    };
    const onVisibility = () => (document.visibilityState === "visible" ? start() : stop());
    if (document.visibilityState !== "hidden") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [now]);
  return () => (total.current + (since.current === null ? 0 : now() - since.current)) / 1000;
}

const defaultNow = () => performance.now();

export function ReviewCard({ card, busy = false, onApprove, onEdit, onReject, onLater, now = defaultNow }: ReviewCardProps) {
  const [acked, setAcked] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const seconds = useVisibleSeconds(now);

  const open = card.flags.filter((f) => !acked.has(f.id)).length;
  const blocked = card.blocking.length > 0 || !card.can_approve || card.root_hash === null;
  const canApprove = !blocked && open === 0 && !busy;

  const toggle = (id: string) =>
    setAcked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  let hint = "Approving sends nothing; the piece goes out on its slot.";
  if (card.blocking.length > 0) hint = "This bundle cannot be approved yet. See what blocks it above.";
  else if (!card.can_approve) hint = "This bundle is not waiting for review.";
  else if (open > 0) hint = `Mark ${open} flag${open === 1 ? "" : "s"} as read to approve.`;

  return (
    <article className="card" aria-labelledby={`card-${card.bundle_id}`}>
      <header className="card-head">
        <h1 id={`card-${card.bundle_id}`} className="card-title">
          {card.variants.map((v) => v.channel).join(" and ") || "Empty bundle"}
        </h1>
        {card.why ? <p className="card-why">Why now: {card.why}</p> : null}
      </header>

      {card.blocking.length > 0 ? (
        <section className="panel panel-block" aria-label="What blocks approval">
          <h2>Cannot be approved yet</h2>
          <ul>
            {card.blocking.map((b, i) => (
              <li key={`${b.check_name}-${b.variant_id ?? "all"}-${i}`}>
                <span className="check-name">{b.check_name}</span>
                <span className="check-kind">{b.kind === "fail" ? "failed" : b.kind === "not_checked" ? "not checked" : "problem"}</span>
                {b.reason ? <p>{b.reason}</p> : null}
                <p className="next-step">{NEXT_STEP[b.kind]}</p>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {card.variants.map((v) => (
        <section key={v.id} className="piece" aria-label={`${v.channel} piece`}>
          <div className="piece-meta">
            <span className="chip">{v.channel}</span>
            <span className="chip quiet">{v.language}</span>
            <span className="chip quiet">version {v.version}</span>
          </div>
          {v.media.map((m) =>
            m.url && m.mime.startsWith("image/") ? (
              // eslint-disable-next-line @next/next/no-img-element -- bytes come from the authenticated media route
              <img key={m.sha256} className="piece-media" src={m.url} alt={m.alt_text ?? "Image with no description"} />
            ) : (
              <p key={m.sha256} className="piece-media-missing">
                {m.kind} file ({m.mime}) {m.url ? <a href={m.url}>open</a> : "not viewable here"}
              </p>
            ),
          )}
          {editing === v.id ? (
            <form
              className="edit"
              onSubmit={(e) => {
                e.preventDefault();
                onEdit(v.id, draft);
              }}
            >
              <label htmlFor={`edit-${v.id}`}>Edit the text. Facts stay as slots like {"{{C-0001}}"}.</label>
              <textarea id={`edit-${v.id}`} value={draft} onChange={(e) => setDraft(e.target.value)} rows={6} />
              <div className="row">
                <button type="submit" className="button primary" disabled={busy || !draft.trim()}>
                  Save and recheck
                </button>
                <button type="button" className="button" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <p className="piece-text" data-testid="piece-text">
              {v.text}
            </p>
          )}
          {v.claims.length > 0 ? (
            <details className="facts">
              <summary>Facts used ({v.claims.length})</summary>
              <ul>
                {v.claims.map((c) => (
                  <li key={c.code}>
                    <span className="check-name">{c.code}</span> {c.value}
                    {c.unit ? ` ${c.unit}` : ""} <span className="chip quiet">{c.state}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          {editing !== v.id ? (
            <button
              type="button"
              className="link-button"
              onClick={() => {
                setEditing(v.id);
                setDraft(v.source_text);
              }}
            >
              Edit this piece
            </button>
          ) : null}
        </section>
      ))}

      {card.flags.length > 0 ? (
        <section className="panel panel-flag" aria-label="Flags to read">
          <h2>Read before approving</h2>
          <ul>
            {card.flags.map((f) => (
              <li key={f.id} className={acked.has(f.id) ? "flag read" : "flag"}>
                <div>
                  <span className="check-name">{f.check_name}</span>
                  <p>{f.reason ?? "A check asked a person to look at this."}</p>
                </div>
                <button
                  type="button"
                  className="button small"
                  aria-pressed={acked.has(f.id)}
                  onClick={() => toggle(f.id)}
                >
                  {acked.has(f.id) ? "Read" : "Mark as read"}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {rejecting ? (
        <section className="panel" aria-label="Reason for rejecting">
          <h2>Why not?</h2>
          <div className="reasons">
            {REJECTION_REASONS.map((r) => (
              <button key={r.value} type="button" className="button" disabled={busy} onClick={() => onReject(r.value)}>
                {r.label}
              </button>
            ))}
          </div>
          <button type="button" className="link-button" onClick={() => setRejecting(false)}>
            Keep it
          </button>
        </section>
      ) : null}

      <footer className="card-foot">
        <p className="hash">
          You approve exactly version <code data-testid="root-hash" title={card.root_hash ?? undefined}>{shortHash(card.root_hash)}</code>
        </p>
        <p className="hint" aria-live="polite">
          {hint}
        </p>
        <div className="row">
          <button
            type="button"
            className="button primary big"
            disabled={!canApprove}
            onClick={() => onApprove([...acked], Math.round(seconds() * 10) / 10)}
          >
            Approve
          </button>
          <button type="button" className="button" disabled={busy || card.state !== "in_review"} onClick={() => setRejecting(true)}>
            Reject
          </button>
          {onLater ? (
            <button type="button" className="button quiet" onClick={onLater}>
              Later
            </button>
          ) : null}
        </div>
      </footer>
    </article>
  );
}
