"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { ReviewCard } from "@/components/ReviewCard";
import { Failure, Loading } from "@/components/States";
import { ApiError, get, post } from "@/lib/api";
import { type Outcome, refusalFor } from "@/lib/refusal";
import type { BundleSummary, Card } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";

export default function ReviewCardPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const path = `/api/review/bundles/${encodeURIComponent(id)}`;
  const [load, reload] = useLoad<Card>(path);
  const [list] = useLoad<{ bundles: BundleSummary[] }>("/api/review/bundles");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const ids = list.state === "ready" ? list.data.bundles.map((b) => b.bundle_id) : [];
  const at = ids.indexOf(id);
  const nextId = ids.find((b) => b !== id) ?? null;
  const goNext = () => router.push(nextId ? `/review/${nextId}` : "/review");

  const act = async (fn: () => Promise<string>, onRefused?: (e: ApiError) => Promise<Outcome | null>) => {
    setBusy(true);
    setOutcome(null);
    try {
      setOutcome({ tone: "good", text: await fn() });
    } catch (e) {
      const refused = e instanceof ApiError && onRefused ? await onRefused(e) : null;
      setOutcome(refused ?? { tone: "bad", text: e instanceof ApiError ? e.message : "Something went wrong. Nothing changed." });
    } finally {
      setBusy(false);
    }
  };

  if (load.state === "loading") return <Loading what="this card" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;

  return (
    <div className="card-page">
      <div className="card-top">
        <Link href="/review" className="back">
          All cards
        </Link>
      </div>
      <ReviewCard
        key={load.data.root_hash ?? id}
        card={load.data}
        position={at >= 0 ? `${at + 1} of ${ids.length}` : undefined}
        busy={busy || outcome?.tone === "good"}
        onApprove={(acked, seconds) =>
          act(
            async () => {
              await post(`${path}/approve`, { acknowledged_flag_ids: acked, seconds_on_screen: seconds });
              return "Approved. It posts on its slot.";
            },
            async (e) => {
              if (e.status !== 409) return null;
              // Showing the card again records it, so the next approval is checked against what is shown now.
              const fresh = await get<Card>(path).catch(() => null);
              reload();
              return refusalFor(fresh, acked);
            },
          )
        }
        onEdit={(variantId, text) =>
          act(async () => {
            await post(`/api/review/variants/${encodeURIComponent(variantId)}/edit`, { text });
            return "Saved. The checks run again before this comes back to you.";
          })
        }
        onReject={(reason) =>
          act(async () => {
            await post(`${path}/reject`, { reason });
            return "Rejected. The slot is filled from the evergreen bench.";
          })
        }
        onLater={goNext}
      />
      {outcome ? (
        <div className={`toast ${outcome.tone}`} role={outcome.tone === "bad" ? "alert" : "status"}>
          <p>{outcome.tone === "bad" ? <strong>{outcome.text}</strong> : outcome.text}</p>
          {outcome.items && outcome.items.length > 0 ? (
            <ul>
              {outcome.items.map((t, i) => (
                <li key={`${t}-${i}`}>{t}</li>
              ))}
            </ul>
          ) : null}
          {outcome.next ? <p>{outcome.next}</p> : null}
          <div className="row">
            {outcome.tone === "good" ? (
              <button type="button" className="button primary" onClick={goNext}>
                {nextId ? "Next card" : "Back to the deck"}
              </button>
            ) : (
              <button type="button" className="button" onClick={() => setOutcome(null)}>
                Close
              </button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
