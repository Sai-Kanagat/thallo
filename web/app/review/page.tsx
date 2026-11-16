"use client";

import Link from "next/link";

import { Empty, Failure, Loading } from "@/components/States";
import type { BundleSummary } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";

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
  return (
    <section>
      <h1 className="page-title">
        {bundles.length} to review
      </h1>
      <ol className="deck">
        {bundles.map((b, i) => (
          <li key={b.bundle_id}>
            <Link href={`/review/${b.bundle_id}`} className="deck-item">
              <span className="deck-n">{i + 1}</span>
              <span className="deck-channels">{b.channels.join(", ")}</span>
              <span className="deck-count">
                {b.variant_count} piece{b.variant_count === 1 ? "" : "s"}
              </span>
            </Link>
          </li>
        ))}
      </ol>
    </section>
  );
}
