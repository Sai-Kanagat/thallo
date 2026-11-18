"use client";

import Link from "next/link";

import { Empty, Failure, Loading } from "@/components/States";
import type { BundleSummary } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";
import { channelList, slotDay } from "@/lib/words";

function statusWords(b: BundleSummary): { cls: string; text: string } {
  if (b.status === "blocked") return { cls: "blocked", text: "Blocked" };
  if (b.status === "flag") {
    const n = b.flag_count ?? 1;
    return { cls: "flag", text: `${n} flag${n === 1 ? "" : "s"} to read` };
  }
  if (b.status === "ready") return { cls: "ready", text: "Ready" };
  return { cls: "", text: "To review" };
}

function tally(bundles: BundleSummary[]): string | null {
  if (bundles.some((b) => b.status === undefined)) return null;
  const n = (s: BundleSummary["status"]) => bundles.filter((b) => b.status === s).length;
  const parts = [
    n("ready") ? `${n("ready")} ready` : null,
    n("flag") ? `${n("flag")} with a flag to read` : null,
    n("blocked") ? `${n("blocked")} blocked` : null,
  ].filter(Boolean);
  return parts.join(", ");
}

export default function ReviewList() {
  const [load, reload] = useLoad<{ bundles: BundleSummary[] }>("/api/review/bundles");
  if (load.state === "loading") return <Loading what="the review deck" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const bundles = load.data.bundles;
  if (bundles.length === 0) {
    return (
      <Empty title="Nothing to review">
        <p>New pieces land here after their checks pass. Tuesday is the usual day.</p>
      </Empty>
    );
  }
  const summary = tally(bundles);
  return (
    <section className="narrow-col">
      <div className="deck-head">
        <h1 className="page-title">{bundles.length} to review</h1>
        {summary ? <p className="deck-tally">{summary}</p> : null}
      </div>
      <ol className="deck">
        {bundles.map((b) => {
          const s = statusWords(b);
          const day = slotDay(b.slot?.day);
          const title = b.headline ?? `${channelList(b.channels)} piece`;
          return (
            <li key={b.bundle_id}>
              <Link href={`/review/${b.bundle_id}`} className="deck-item">
                {b.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element -- bytes come from the authenticated media route
                  <img className="deck-thumb" src={b.thumbnail_url} alt="" loading="lazy" />
                ) : (
                  <span className="deck-thumb none" aria-hidden="true">
                    Text
                  </span>
                )}
                <span className="deck-body">
                  <span className="deck-headline">{title}</span>
                  <span className="deck-meta">
                    <span>{channelList(b.channels)}</span>
                    <span>{day ?? "No slot yet"}</span>
                    <span className={`chip ${s.cls}`}>{s.text}</span>
                  </span>
                </span>
                <span className={`chip deck-status ${s.cls}`}>{s.text}</span>
              </Link>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
