"use client";

import Link from "next/link";

import { Empty, Failure, Loading } from "@/components/States";
import { useLoad } from "@/lib/useLoad";

import styles from "../leads/leads.module.css";

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

const STAGE: Record<string, string> = {
  new: "New and qualifying",
  nurturing: "In a follow-up sequence",
  contacted: "Talking",
  booked: "Meeting booked",
  held: "Meeting happened",
  won: "Won",
  lost: "Lost",
  parked: "Parked, low fit",
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
    <section className={styles.detail}>
      <div className={styles.section}>
        <h1 className="page-title">Pipeline</h1>
        <p className="page-sub">What happened after each enquiry, from the taps on the lead desk.</p>
        <div className={styles.two}>
          <span><b>{b.booked_conversations}</b> booked conversations</span>
          <span><b>{b.won_count}</b> won</span>
        </div>
        <table className={styles.table}>
          <tbody>
            {(b.stages ?? []).map((s) => (
              <tr key={s.stage}>
                <td>{STAGE[s.stage] ?? s.stage}</td>
                <td>{s.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={styles.section}>
        <h2>Meetings to log</h2>
        {b.unlogged_count ? (
          <ul className={styles.grid}>
            {(b.unlogged ?? []).map((u) => (
              <li key={u.booking_id}>
                <Link href={`/leads/${u.lead_id}`} className={styles.row}>
                  <span className={styles.name}>{u.kind.replace(/_/g, " ")}</span>
                  <span className={styles.clock}>{new Date(u.starts_at).toLocaleString()}</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="page-sub">Every past meeting is logged.</p>
        )}
      </div>
    </section>
  );
}
