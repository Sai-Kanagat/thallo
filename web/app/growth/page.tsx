"use client";

import { useState } from "react";

import { Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";
import { channelList, channelName } from "@/lib/words";

import type { GrowthModelView, GrowthView, PlayView, Summary } from "./types";

const ALL_CHANNELS = [
  "google_business_profile", "whatsapp", "facebook", "instagram", "linkedin_personal", "linkedin_company",
  "email", "newsletter", "website", "youtube", "search_ads", "social_ads",
];

function words(slug: string): string {
  const w = slug.replaceAll("_", " ");
  return w.charAt(0).toUpperCase() + w.slice(1);
}

function pct(share: string): string {
  return `${Math.round(Number(share) * 1000) / 10}%`;
}

function money(amount: string, currency: string): string {
  return `${currency} ${amount}`;
}

function TagChip({ tag }: { tag: string }) {
  return <span className={tag === "measured" ? "chip ready" : "chip flag"}>{tag === "measured" ? "Measured" : "Guessed"}</span>;
}

type Form = {
  play_code: string;
  margin_per_won: string;
  share: string;
  rates: Record<string, { rate: string; low: string; high: string }>;
};

function initialForm(g: GrowthView): Form {
  const m = g.model;
  const rates: Form["rates"] = {};
  for (const s of g.stages) {
    const saved = m?.stages.find((x) => x.name === s.name);
    rates[s.name] = { rate: saved?.rate ?? "", low: saved?.low ?? "", high: saved?.high ?? "" };
  }
  return {
    play_code: m?.play_code ?? g.plays[0]?.code ?? "A",
    margin_per_won: m?.margin_per_won ?? "",
    share: m?.share_of_margin ?? g.default_share,
    rates,
  };
}

function toBody(f: Form) {
  const rates: Record<string, { rate: string; low?: string; high?: string }> = {};
  for (const [name, r] of Object.entries(f.rates)) {
    if (!r.rate) continue;
    rates[name] = { rate: r.rate, ...(r.low ? { low: r.low } : {}), ...(r.high ? { high: r.high } : {}) };
  }
  return { play_code: f.play_code, margin_per_won: f.margin_per_won, share_of_margin: f.share, rates };
}

function SummaryPanel({ s, guessLabel }: { s: Summary; guessLabel: string }) {
  return (
    <section className="mix-panel" aria-label="Allowable cost">
      <h2>
        Allowable cost per booked conversation: {money(s.allowable_cost, s.currency)} <TagChip tag={s.tag} />
      </h2>
      <p className="fine">
        Range {money(s.allowable_low, s.currency)} to {money(s.allowable_high, s.currency)}. {s.formula}.
      </p>
      {s.tag === "guessed" ? <p className="fine">Built on guessed rates. {guessLabel}</p> : null}
      <h3>Target cost by channel role</h3>
      <ul className="mix">
        {s.channel_targets.map((t) => (
          <li key={t.role}>
            <span className="mix-label">{t.multiple} x A</span>
            <span>
              {t.label}: at most {money(t.max_cost, s.currency)} <TagChip tag={t.tag} />
            </span>
          </li>
        ))}
      </ul>
      <p className="fine">
        {s.stop_rule} Here that is {money(s.stop_after_spend, s.currency)}.
      </p>
    </section>
  );
}

function ModelForm({ g, onSaved }: { g: GrowthView; onSaved: () => void }) {
  const [f, setF] = useState<Form>(() => initialForm(g));
  const [preview, setPreview] = useState<Summary | null>(null);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);

  const setRate = (name: string, key: "rate" | "low" | "high", v: string) =>
    setF({ ...f, rates: { ...f.rates, [name]: { rate: "", low: "", high: "", ...f.rates[name], [key]: v } } });

  const run = async (path: string, done: string) => {
    setMessage(null);
    try {
      const r = await post<{ summary?: Summary; model?: GrowthModelView }>(path, toBody(f));
      setPreview(r.summary ?? r.model?.summary ?? null);
      setMessage({ text: done, bad: false });
      if (r.model) onSaved();
    } catch (e) {
      setMessage({ text: e instanceof ApiError ? e.message : "That did not work.", bad: true });
    }
  };

  return (
    <section aria-label="Growth model form">
      <h2>How this business gets customers</h2>
      <p className="fine">
        Every rate you type is stored as a guess. It turns into a measured rate only after {g.measured_after} outcomes.
      </p>
      <div className="field">
        <label htmlFor="g-play">Play</label>
        <select id="g-play" value={f.play_code} onChange={(e) => setF({ ...f, play_code: e.target.value })}>
          {g.plays.filter((p) => !p.invalid).map((p) => (
            <option key={p.code} value={p.code}>
              {p.code}. {p.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="g-margin">Margin per won deal ({g.currency})</label>
        <input id="g-margin" inputMode="decimal" value={f.margin_per_won}
               onChange={(e) => setF({ ...f, margin_per_won: e.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="g-share">Share of margin you will spend to win a deal (0 to 1)</label>
        <input id="g-share" inputMode="decimal" value={f.share} onChange={(e) => setF({ ...f, share: e.target.value })} />
      </div>
      {g.stages.map((s) => (
        <fieldset key={s.name} className="field">
          <legend>
            {s.label}
            {s.required ? "" : " (optional)"}
          </legend>
          <div className="row">
            {(["rate", "low", "high"] as const).map((k) => (
              <label key={k}>
                {k === "rate" ? "Best guess" : k === "low" ? "Low" : "High"}{" "}
                <input inputMode="decimal" size={6} value={f.rates[s.name]?.[k] ?? ""}
                       aria-label={`${s.label}, ${k}`} onChange={(e) => setRate(s.name, k, e.target.value)} />
              </label>
            ))}
          </div>
        </fieldset>
      ))}
      <div className="row">
        <button type="button" className="button small" onClick={() => run("/api/growth/preview", "Worked out, not saved.")}>
          Work it out
        </button>
        {g.can_edit ? (
          <button type="button" className="button small primary" onClick={() => run("/api/growth/model", "Saved as a new version.")}>
            Save
          </button>
        ) : null}
      </div>
      {message ? <p className={message.bad ? "note bad" : "note"} role="status">{message.text}</p> : null}
      {preview ? <SummaryPanel s={preview} guessLabel={g.guess_label} /> : null}
    </section>
  );
}

function PlayEditor({ p, onSaved }: { p: PlayView; onSaved: () => void }) {
  const [name, setName] = useState(p.name);
  const [booked, setBooked] = useState(p.booked_definition);
  const [channels, setChannels] = useState<string[]>(p.channels);
  const [mix, setMix] = useState<Record<string, string>>(p.pillar_mix);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);

  const save = async () => {
    setMessage(null);
    try {
      await post(`/api/growth/plays/${encodeURIComponent(p.code)}`, {
        name, booked_definition: booked, channels, pillar_mix: mix,
      });
      setMessage({ text: "Saved. It passed the region policy check.", bad: false });
      onSaved();
    } catch (e) {
      setMessage({ text: e instanceof ApiError ? e.message : "That did not save.", bad: true });
    }
  };

  return (
    <div aria-label={`Edit play ${p.code}`}>
      <div className="field">
        <label htmlFor={`p-${p.code}-name`}>Name</label>
        <input id={`p-${p.code}-name`} value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor={`p-${p.code}-booked`}>What counts as a booked conversation</label>
        <input id={`p-${p.code}-booked`} value={booked} onChange={(e) => setBooked(e.target.value)} />
      </div>
      <fieldset className="field">
        <legend>Channels</legend>
        {ALL_CHANNELS.map((c) => (
          <label key={c}>
            <input type="checkbox" checked={channels.includes(c)}
                   onChange={(e) => setChannels(e.target.checked ? [...channels, c] : channels.filter((x) => x !== c))} />{" "}
            {channelName(c)}
          </label>
        ))}
      </fieldset>
      <fieldset className="field">
        <legend>Pillar shares (sum to 1, each at least 0.10)</legend>
        {Object.keys(mix).map((k) => (
          <label key={k}>
            {words(k)}{" "}
            <input inputMode="decimal" size={6} value={mix[k]} onChange={(e) => setMix({ ...mix, [k]: e.target.value })} />
          </label>
        ))}
      </fieldset>
      <button type="button" className="button small primary" onClick={save}>
        Check and save
      </button>
      {message ? <p className={message.bad ? "note bad" : "note"} role="status">{message.text}</p> : null}
    </div>
  );
}

function PlayCard({ p, canEdit, onSaved }: { p: PlayView; canEdit: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  if (p.invalid) {
    return (
      <li className="mix-panel">
        <h3>{p.code}</h3>
        <p className="note bad">This saved play no longer validates: {p.invalid.join(" ")}</p>
      </li>
    );
  }
  const policy = p.policy;
  return (
    <li className="mix-panel">
      <h3>
        {p.code}. {p.name}
      </h3>
      <p className="row">
        <span className="chip">{words(p.status)}</span>
        <span className="chip flag">Numbers are guesses</span>
        <span className={policy.status === "ok" ? "chip ready" : "chip blocked"}>
          {policy.status === "ok" ? `Fits ${policy.region_code} policy` : policy.status === "no_policy"
            ? `Not validated: no ${policy.region_code} policy on file` : `Contradicts ${policy.region_code} policy`}
        </span>
        {p.saved ? <span className="chip info">Saved for this business</span> : null}
      </p>
      {policy.problems.length && policy.status === "contradicts" ? <p className="note bad">{policy.problems.join(" ")}</p> : null}
      <p>{p.fits}</p>
      <p className="fine">Channels: {channelList(p.channels)}</p>
      <p className="fine">Booked conversation: {p.booked_definition}</p>
      <p className="fine">First bottleneck to test: {p.bottlenecks_in_order[0]}</p>
      <p className="fine">Headline metric: {words(p.headline_metric)}</p>
      <ul className="mix" aria-label={`Pillar mix for play ${p.code}`}>
        {Object.entries(p.pillar_mix).map(([k, v]) => (
          <li key={k}>
            <span className="mix-label">{words(k)}</span>
            <progress max={1} value={Number(v)} aria-label={`${words(k)} share`} />
            <span className="mix-num">{pct(v)} of slots, a starting guess</span>
          </li>
        ))}
      </ul>
      <p className="fine">{p.evidence_label}</p>
      {canEdit ? (
        <button type="button" className="button small" onClick={() => setEditing(!editing)}>
          {editing ? "Close editor" : "Edit play"}
        </button>
      ) : null}
      {editing ? <PlayEditor p={p} onSaved={onSaved} /> : null}
    </li>
  );
}

export default function GrowthPage() {
  const [load, reload] = useLoad<GrowthView>("/api/growth");
  if (load.state === "loading") return <Loading what="the growth model" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const g = load.data;

  return (
    <section className="narrow-col">
      <h1 className="page-title">Growth model</h1>
      <p className="page-sub">
        {g.model
          ? `Version ${g.model.version}, play ${g.model.play_code ?? "none"}. ${g.model.bottleneck ?? ""}`
          : "No growth model yet. Answer the form below; every number starts as a guess."}
      </p>
      {g.model ? <SummaryPanel s={g.model.summary} guessLabel={g.guess_label} /> : null}
      {g.model ? (
        <ul className="mix" aria-label="Stage rates">
          {g.model.stages.map((s) => (
            <li key={s.name}>
              <span className="mix-label">{pct(s.rate)}</span>
              <span>
                {s.label} ({pct(s.low)} to {pct(s.high)}) <TagChip tag={s.tag} />{" "}
                {s.tag === "guessed" ? `${s.outcomes} of ${g.measured_after} outcomes so far` : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <ModelForm key={g.model?.version ?? 0} g={g} onSaved={reload} />
      <h2>Play library</h2>
      <ul className="conns">
        {g.plays.map((p) => (
          <PlayCard key={p.code} p={p} canEdit={g.can_edit} onSaved={reload} />
        ))}
      </ul>
      {g.open_questions.length ? (
        <section aria-label="Open questions">
          <h2>Open questions</h2>
          <ul>
            {g.open_questions.map((q) => (
              <li key={q} className="fine">
                {q}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
