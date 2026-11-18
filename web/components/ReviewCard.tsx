"use client";

import { useEffect, useRef, useState } from "react";

import { shortHash } from "@/lib/api";
import { NEXT_STEP } from "@/lib/refusal";
import { type Card, REJECTION_REASONS } from "@/lib/types";
import { channelList, channelName, checkWords, detailText, groupChecks, slotDay } from "@/lib/words";

// One card per bundle, in the order a person decides: the piece exactly as it will post, then
// what needs their attention, then the decision. Everything shown is what the server hashed; the
// client sends back only the flag ids it acknowledged and the seconds this card was visible.
// All text is untrusted and rendered as text.

export type ReviewCardProps = {
  card: Card;
  busy?: boolean;
  onApprove: (acknowledgedFlagIds: string[], secondsOnScreen: number) => void;
  onEdit: (variantId: string, text: string) => void;
  onReject: (reason: string) => void;
  onLater?: () => void;
  position?: string;
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

function CopyHash({ hash }: { hash: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="link-button"
      onClick={() => {
        void navigator.clipboard?.writeText(hash).then(() => setCopied(true), () => undefined);
      }}
    >
      {copied ? "Copied" : "Copy the full version id"}
    </button>
  );
}

export function ReviewCard({ card, busy = false, onApprove, onEdit, onReject, onLater, position, now = defaultNow }: ReviewCardProps) {
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

  let hint = "Approving sends nothing now. It posts on its slot.";
  if (card.blocking.length > 0) hint = "This cannot be approved yet. What to fix is under the piece.";
  else if (!card.can_approve) hint = "This bundle is not waiting for review.";
  else if (open > 0) hint = `Mark ${open} flag${open === 1 ? "" : "s"} as read to approve.`;

  const day = slotDay(card.slot?.day);
  const channels = channelList(card.variants.map((v) => v.channel));
  const claims = card.variants.flatMap((v) => v.claims);

  return (
    <article className="card" aria-labelledby={`card-${card.bundle_id}`}>
      <header className="card-head">
        <div className="card-meta">
          <h1 id={`card-${card.bundle_id}`} className="sr-only">
            {channels} piece{day ? ` for ${day}` : ""}
          </h1>
          <span>{channels}</span>
          {day ? <span>{day}</span> : <span>No slot yet</span>}
          {position ? <span className="card-pos">{position}</span> : null}
        </div>
        {card.reason ? <p className="card-reason">{card.reason}</p> : null}
      </header>

      <div className="card-piece">
        {card.variants.length === 0 ? <p className="post-media-missing">This bundle has no pieces.</p> : null}
        {card.variants.map((v) => (
          <section key={v.id} className="post" aria-label={`${channelName(v.channel)} piece as it will post`}>
            <div className="post-head">
              <strong>{channelName(v.channel)}</strong>
              <span>{v.language.toUpperCase()}</span>
              <span>version {v.version}</span>
            </div>
            {v.media.map((m) =>
              m.url && m.mime.startsWith("image/") ? (
                // eslint-disable-next-line @next/next/no-img-element -- bytes come from the authenticated media route
                <img key={m.sha256} className="post-media" src={m.url} alt={m.alt_text ?? "Image with no description"} />
              ) : (
                <p key={m.sha256} className="post-media-missing">
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
                <textarea id={`edit-${v.id}`} value={draft} onChange={(e) => setDraft(e.target.value)} rows={8} />
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
              <>
                <p className="post-text" data-testid="piece-text">
                  {v.text}
                </p>
                <div className="post-foot">
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
                </div>
              </>
            )}
          </section>
        ))}
      </div>

      <div className="card-side">
        <section className="attention" aria-label="What needs your attention">
          {card.blocking.length > 0 ? (
            <>
              <h2>Fix before it can be approved</h2>
              <ul>
                {groupChecks(card.blocking).map(({ first: b, details }) => {
                  const w = checkWords(b);
                  return (
                    <li key={b.check_name} className="item item-blocked">
                      <span className="item-title">{w.label}</span>
                      <p className="item-explain">{w.explain}</p>
                      {details.length > 0 ? <p className="item-detail">{details.join(". ")}</p> : null}
                      <p className="item-next">{NEXT_STEP[b.kind]}</p>
                      <span className="check-code">{b.check_name}</span>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : null}
          {card.flags.length > 0 ? (
            <>
              <h2>Read before approving</h2>
              <ul>
                {card.flags.map((f) => {
                  const w = checkWords(f);
                  const read = acked.has(f.id);
                  return (
                    <li key={f.id} className={read ? "item item-flag read" : "item item-flag"}>
                      <div className="item-row">
                        <div>
                          <span className="item-title">{w.label}</span>
                          <p className="item-explain">{w.explain}</p>
                          {f.reason ? <p className="item-detail">{detailText(f.reason)}</p> : null}
                          <span className="check-code">{f.check_name}</span>
                        </div>
                        <button type="button" className="button small" aria-pressed={read} onClick={() => toggle(f.id)}>
                          {read ? "Read" : "Mark as read"}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : null}
          {card.blocking.length === 0 && card.flags.length === 0 && card.can_approve ? (
            <p className="all-clear">Every check passed. Nothing to read first.</p>
          ) : null}
        </section>

        {rejecting ? (
          <section className="reject-panel" aria-label="Reason for rejecting">
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

        <div className="approve-bar">
          <p className="hint" aria-live="polite">
            {hint}
          </p>
          <div className="actions">
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
        </div>

        <div className="more">
          {claims.length > 0 ? (
            <details>
              <summary>Facts used ({claims.length})</summary>
              <div>
                <ul>
                  {claims.map((c) => (
                    <li key={c.code}>
                      {c.value}
                      {c.unit ? ` ${c.unit}` : ""} <span className="chip">{c.state}</span>{" "}
                      <span className="check-code">{c.code}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </details>
          ) : null}
          <details>
            <summary>How this was chosen</summary>
            <div>
              {card.why ? <p className="why-tech">{card.why}</p> : <p className="why-tech">No selection notes for this piece.</p>}
              <div className="hash-row">
                <span>
                  You approve exactly version{" "}
                  <code data-testid="root-hash" title={card.root_hash ?? undefined}>
                    {shortHash(card.root_hash)}
                  </code>
                </span>
                {card.root_hash ? <CopyHash hash={card.root_hash} /> : null}
              </div>
            </div>
          </details>
        </div>
      </div>
    </article>
  );
}
