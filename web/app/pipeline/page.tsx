"use client";

import { Empty, Failure, Loading } from "@/components/States";
import { useLoad } from "@/lib/useLoad";

type Board = {
  set_up: boolean;
  stages?: { stage: string; count: number }[];
  booked_conversations?: number;
  booked_this_week?: number;
  unlogged_count?: number;
  unlogged?: { booking_id: string; lead_id: string; kind: string; starts_at: string }[];
  won_count?: number;
  won_amount?: string;
  lost_count?: number;
};

export default function PipelinePage() {
  const [load, reload] = useLoad<Board>("/api/nurture/pipeline");
  if (load.state === "loading") return <Loading what="the pipeline" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const b = load.data;
  if (!b.set_up) {
    return (
      <Empty title="The pipeline is not set up">
        <p>It fills once the lead desk has a play.</p>
      </Empty>
    );
  }
  return (
    <section>
      <h1 className="page-title">Pipeline</h1>
      <p className="page-sub">
        {b.booked_conversations} booked conversations, {b.booked_this_week} this week. {b.won_count} won,{" "}
        {b.lost_count} lost.
      </p>
      <ul>
        {(b.stages ?? []).map((s) => (
          <li key={s.stage}>
            <span className="chip">{s.stage}</span> <b>{s.count}</b>
          </li>
        ))}
      </ul>
      {b.unlogged_count ? (
        <>
          <h2>Not logged yet</h2>
          <p className="page-sub">These meetings are past. Mark each one held, no show, won or lost on the lead.</p>
          <ul>
            {(b.unlogged ?? []).map((u) => (
              <li key={u.booking_id}>
                <a href={`/leads/${u.lead_id}`}>
                  {u.kind.replace("_", " ")}, {new Date(u.starts_at).toLocaleString()}
                </a>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
