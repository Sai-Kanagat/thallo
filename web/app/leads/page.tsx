"use client";

import Link from "next/link";

import { Empty, Failure, Loading } from "@/components/States";
import { useLoad } from "@/lib/useLoad";

import styles from "./leads.module.css";
import type { LeadList } from "./types";
import { ROUTE, clockText } from "./words";

export default function LeadsPage() {
  const [load, reload] = useLoad<LeadList>("/api/leads");
  if (load.state === "loading") return <Loading what="the lead desk" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const { set_up, leads } = load.data;
  if (!set_up) {
    return (
      <Empty title="The lead desk is not set up">
        <p>Your operator picks the play and adds your service area and stock list first.</p>
      </Empty>
    );
  }
  if (leads.length === 0) {
    return (
      <Empty title="No enquiries yet">
        <p>Chats, calls and forms with a tracked code land here with a reply clock.</p>
      </Empty>
    );
  }
  return (
    <section>
      <h1 className="page-title">Enquiries</h1>
      <p className="page-sub">Fit says how well they match. Heat says how recently they acted. Two numbers, kept apart.</p>
      <ul className={styles.grid}>
        {leads.map((l) => (
          <li key={l.id}>
            <Link href={`/leads/${l.id}`} className={styles.row}>
              <span className={styles.name}>{l.name}</span>
              <span className={styles.nums}>
                <span><span className="sr-only">Fit </span><b>{l.fit}</b> fit</span>
                <span><span className="sr-only">Heat </span><b>{Math.round(l.heat)}</b> heat</span>
              </span>
              <span className={styles.meta}>
                {l.route ? <span className={l.route === "hot" ? "chip info" : "chip"}>{ROUTE[l.route]}</span> : null}
                <span className="chip">{l.state.replace("_", " ")}</span>
                {!l.may_message ? <span className="chip flag">Personal reply only</span> : null}
                {l.drafts > 0 ? <span className="chip">{l.drafts} to send</span> : null}
              </span>
              <span className={l.clock.overdue ? `${styles.clock} ${styles.overdue}` : styles.clock}>
                {clockText(l.clock)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
