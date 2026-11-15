"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { ReviewCard } from "@/components/ReviewCard";
import { Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import type { BundleSummary, Card } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";

type Outcome = { tone: "good" | "bad"; text: string };

export default function ReviewCardPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [load, reload] = useLoad<Card>(`/api/review/bundles/${encodeURIComponent(id)}`);
  const [list] = useLoad<{ bundles: BundleSummary[] }>("/api/review/bundles");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const nextId =
    list.state === "ready" ? list.data.bundles.map((b) => b.bundle_id).find((b) => b !== id) ?? null : null;
  const goNext = () => router.push(nextId ? `/review/${nextId}` : "/review");

  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    setOutcome(null);
    try {
      setOutcome({ tone: "good", text: await fn() });
    } catch (e) {
      setOutcome({ tone: "bad", text: e instanceof ApiError ? e.message : "Something went wrong. Nothing changed." });
    } finally {
      setBusy(false);
    }
  };

  if (load.state === "loading") return <Loading what="this card" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;

  return (
    <div className="card-page">
      <Link href="/review" className="back">
        All cards
      </Link>
      <ReviewCard
        card={load.data}
        busy={busy || outcome?.tone === "good"}
        onApprove={(ids, seconds) =>
          act(async () => {
            await post(`/api/review/bundles/${encodeURIComponent(id)}/approve`, {
              acknowledged_flag_ids: ids,
              seconds_on_screen: seconds,
            });
            return "Approved. It goes out on its slot.";
          })
        }
        onEdit={(variantId, text) =>
          act(async () => {
            await post(`/api/review/variants/${encodeURIComponent(variantId)}/edit`, { text });
            return "Saved. The checks run again before this comes back to you.";
          })
        }
        onReject={(reason) =>
          act(async () => {
            await post(`/api/review/bundles/${encodeURIComponent(id)}/reject`, { reason });
            return "Rejected. The slot is filled from the evergreen bench.";
          })
        }
        onLater={goNext}
      />
      {outcome ? (
        <div className={`toast ${outcome.tone}`} role={outcome.tone === "bad" ? "alert" : "status"}>
          <p>{outcome.text}</p>
          {outcome.tone === "good" ? (
            <button type="button" className="button primary" onClick={goNext}>
              {nextId ? "Next card" : "Back to the deck"}
            </button>
          ) : (
            <button type="button" className="button" onClick={reload}>
              Reload the card
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}
