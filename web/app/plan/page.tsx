"use client";

import { useState } from "react";

import { Empty, Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import type { Forecast, PlannedSlot, Slot, WeekPlan } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";
import { channelName } from "@/lib/words";

const DAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });

function shiftWeek(start: string, days: number): string {
  const d = new Date(`${start}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function ForecastLine({ f }: { f: Forecast }) {
  return (
    <p className="quiet-text">
      {f.metric} by day {f.day}: {f.low} to {f.high} (baseline {f.baseline_low} to {f.baseline_high}).{" "}
      {f.scored ? `Actual ${f.actual}${f.beat_baseline ? ", beat the baseline." : ", did not beat the baseline."}` : f.label}
    </p>
  );
}

function PlannedDetail({ p }: { p: PlannedSlot }) {
  return (
    <div className="slot-planned">
      <p>
        <strong>{p.idea ?? p.format}</strong> ({p.format}, play {p.play}). {p.reason}
      </p>
      <p className="quiet-text">Measured on {p.target_metric} at day {p.metric_day}.</p>
      {p.forecast ? <ForecastLine f={p.forecast} /> : <p className="quiet-text">{p.no_forecast_reason ?? "No forecast."}</p>}
    </div>
  );
}

export default function PlanPage() {
  const [week, setWeek] = useState<string | null>(null);
  const [load, reload] = useLoad<WeekPlan>(week ? `/api/plan?week=${week}` : "/api/plan");
  const [moving, setMoving] = useState<Slot | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setMessage(null);
    try {
      await fn();
      setMessage(done);
      setMoving(null);
      reload();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };

  if (load.state === "loading") return <Loading what="the week" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const plan = load.data;

  return (
    <section>
      <div className="week-head">
        <button type="button" className="button small" onClick={() => setWeek(shiftWeek(plan.week_start, -7))}>
          Previous
        </button>
        <h1 className="page-title">Week of {DAY.format(new Date(`${plan.week_start}T00:00:00Z`))}</h1>
        <button type="button" className="button small" onClick={() => setWeek(shiftWeek(plan.week_start, 7))}>
          Next
        </button>
      </div>

      {plan.can_edit ? (
        <div className="row">
          {plan.planner === null ? (
            <button type="button" className="button"
                    onClick={() => act(() => post("/api/plan/propose", { week: plan.week_start }), "Week proposed.")}>
              Propose this week
            </button>
          ) : null}
          {plan.slots.length > 0 ? (
            <button type="button" className="button small"
                    onClick={() => act(() => post("/api/plan/mirror", { week: plan.week_start }), "Calendar updated.")}>
              Mirror to calendar
            </button>
          ) : null}
        </div>
      ) : null}
      {plan.mirror ? (
        <p className="quiet-text">
          Calendar: {plan.mirror.provider}, {plan.mirror.status}, {plan.mirror.mirrored} event(s)
          {plan.mirror.detail ? `. ${plan.mirror.detail}` : ""}.
        </p>
      ) : null}

      {plan.planner ? (
        <section className="mix-panel" aria-label="Why this week">
          <h2>Why this week looks like this</h2>
          {plan.planner.reasoning.map((line) => <p key={line}>{line}</p>)}
          {plan.planner.not_doing.length > 0 ? (
            <>
              <h3>Not doing</h3>
              <ul>{plan.planner.not_doing.map((line) => <li key={line}>{line}</li>)}</ul>
            </>
          ) : null}
          {plan.planner.stage_check.ok ? null : (
            <ul className="note bad">{plan.planner.stage_check.problems.map((line) => <li key={line}>{line}</li>)}</ul>
          )}
        </section>
      ) : null}

      <section className="mix-panel" aria-label="Stage mix">
        <h2>Stage mix against the target</h2>
        <ul className="mix">
          {plan.stage_mix.map((m) => (
            <li key={m.stage}>
              <span className="mix-label">{m.stage}</span>
              <progress max={1} value={m.share} aria-label={`${m.stage} share`} />
              <span className="mix-num">
                {Math.round(m.share * 100)}%{m.target !== null ? ` of ${Math.round(m.target * 100)}% target` : " (no target)"}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {plan.slots.length === 0 ? (
        <Empty title="No slots this week">
          <p>The planner proposes next week on Friday.</p>
        </Empty>
      ) : (
        <ol className="days">
          {plan.days.map((day) => {
            const slots = plan.slots.filter((s) => s.day === day);
            return (
              <li key={day} className="day">
                <h2>{DAY.format(new Date(`${day}T00:00:00Z`))}</h2>
                {slots.length === 0 ? <p className="quiet-text">Nothing planned</p> : null}
                <ul>
                  {slots.map((s) => (
                    <li key={s.id} className={`slot slot-${s.state}`}>
                      <div className="slot-main">
                        <span className="chip info">{channelName(s.channel)}</span>
                        <span className="slot-time">{TIME.format(new Date(s.starts_at))}</span>
                        <span className="slot-pillar">{s.pillar ?? "No pillar"}</span>
                        <span className="chip">{s.stage}</span>
                        <span className="chip">{s.state}</span>
                      </div>
                      {s.planned ? <PlannedDetail p={s.planned} /> : null}
                      {plan.can_edit ? (
                        <div className="row">
                          {s.can_move ? (
                            <button type="button" className="button small" onClick={() => setMoving(s)}>
                              Move
                            </button>
                          ) : null}
                          {s.can_kill ? (
                            <button type="button" className="button small danger"
                                    onClick={() => act(() => post(`/api/plan/slots/${s.id}/kill`), "Slot killed.")}>
                              Kill
                            </button>
                          ) : null}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ol>
      )}

      {moving ? (
        <section className="reject-panel" aria-label="Move a slot">
          <h2>Move to</h2>
          <div className="reasons">
            {plan.days.map((day) => (
              <button key={day} type="button" className="button"
                      onClick={() => act(() => post(`/api/plan/slots/${moving.id}/move`, {
                        starts_at: `${day}T${moving.starts_at.slice(11, 19)}+00:00`,
                      }), "Slot moved.")}>
                {DAY.format(new Date(`${day}T00:00:00Z`))}
              </button>
            ))}
          </div>
          <button type="button" className="link-button" onClick={() => setMoving(null)}>
            Keep it where it is
          </button>
        </section>
      ) : null}
      {message ? <p className="note" role="status">{message}</p> : null}
    </section>
  );
}
