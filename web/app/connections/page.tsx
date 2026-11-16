"use client";

import { type FormEvent, useState } from "react";

import { Failure, Loading } from "@/components/States";
import { ApiError, post } from "@/lib/api";
import type { ConnectionRow, ProviderOption } from "@/lib/types";
import { useLoad } from "@/lib/useLoad";

// Keys are write-only: the field is cleared as soon as it is sent and no route ever returns one.
// Settings offered are only those the API allows for the provider; none names a path or a host.

type Listing = { capabilities: { capability: string; connections: ConnectionRow[] }[] };

function AddForm({ capability, options, onDone }: { capability: string; options: ProviderOption[]; onDone: (m: string) => void }) {
  const [provider, setProvider] = useState(options[0]?.provider ?? "");
  const [model, setModel] = useState("");
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const opt = options.find((o) => o.provider === provider);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const body = {
      capability,
      provider,
      model: model || null,
      settings: Object.fromEntries(Object.entries(settings).filter(([, v]) => v !== "")),
      secret: opt?.takes_secret && secret ? secret : null,
    };
    setSecret("");
    try {
      await post("/api/connections", body);
      onDone(`Added ${provider}.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That was not saved.");
    }
  };

  return (
    <form className="add" onSubmit={submit}>
      <label htmlFor={`p-${capability}`}>Provider</label>
      <select id={`p-${capability}`} value={provider} onChange={(e) => setProvider(e.target.value)}>
        {options.map((o) => (
          <option key={o.provider} value={o.provider}>
            {o.provider} ({o.mode})
          </option>
        ))}
      </select>
      {opt?.fixed_origin ? <p className="fine">Sends to {opt.fixed_origin}, set by the operator.</p> : null}
      {opt?.mode === "hosted" || opt?.mode === "local" ? (
        <>
          <label htmlFor={`m-${capability}`}>Model</label>
          <input id={`m-${capability}`} value={model} onChange={(e) => setModel(e.target.value)} autoComplete="off" />
        </>
      ) : null}
      {opt?.settings.map((k) => (
        <div key={k} className="field">
          <label htmlFor={`s-${capability}-${k}`}>{k.replaceAll("_", " ")}</label>
          <input id={`s-${capability}-${k}`} value={settings[k] ?? ""} autoComplete="off"
                 onChange={(e) => setSettings({ ...settings, [k]: e.target.value })} />
        </div>
      ))}
      {opt?.takes_secret ? (
        <>
          <label htmlFor={`k-${capability}`}>API key</label>
          <input id={`k-${capability}`} type="password" autoComplete="off" value={secret}
                 onChange={(e) => setSecret(e.target.value)} />
          <p className="fine">Stored encrypted. Nobody can read it back here.</p>
        </>
      ) : null}
      <button type="submit" className="button primary" disabled={!provider}>
        Add
      </button>
      {error ? <p className="note bad" role="alert">{error}</p> : null}
    </form>
  );
}

function Row({ row, onDone }: { row: ConnectionRow; onDone: (m: string) => void }) {
  const [key, setKey] = useState("");
  const [replacing, setReplacing] = useState(false);
  const run = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn();
      onDone(done);
    } catch (e) {
      onDone(e instanceof ApiError ? e.message : "That did not work.");
    }
  };
  return (
    <li className={row.enabled ? "conn" : "conn off"}>
      <div className="conn-main">
        <span className="conn-name">{row.provider}</span>
        <span className="chip quiet">{row.mode}</span>
        {row.model ? <span className="chip quiet">{row.model}</span> : null}
        {row.role ? <span className="chip quiet">{row.role}</span> : null}
        <span className={`health health-${row.health ?? "unknown"}`}>
          {row.health === "connected" ? "Working" : row.health === "down" ? "Not working" : "Not tested"}
        </span>
      </div>
      <p className="fine">
        Order {row.is_floor ? "last (always there)" : row.priority + 1}. This month {row.cost_this_month_eur} EUR.
        Quota {row.quota ?? "not reported"}. Key {row.has_secret ? "stored" : "none"}.
        {row.fixed_origin ? ` Sends to ${row.fixed_origin}.` : ""}
      </p>
      <div className="row">
        <button type="button" className="button small" onClick={() => run(() => post(`/api/connections/${row.id}/test`), `Tested ${row.provider}.`)}>
          Test
        </button>
        {!row.is_floor ? (
          <>
            <button type="button" className="button small"
                    onClick={() => run(() => post(`/api/connections/${row.id}/enabled`, { enabled: !row.enabled }),
                                       row.enabled ? "Turned off." : "Turned on.")}>
              {row.enabled ? "Turn off" : "Turn on"}
            </button>
            {row.has_secret ? (
              <button type="button" className="button small" onClick={() => setReplacing(!replacing)}>
                Replace key
              </button>
            ) : null}
            <button type="button" className="button small danger"
                    onClick={() => run(() => post(`/api/connections/${row.id}`, undefined, "DELETE"), "Removed.")}>
              Remove
            </button>
          </>
        ) : null}
      </div>
      {replacing ? (
        <form className="row" onSubmit={(e) => {
          e.preventDefault();
          const value = key;
          setKey("");
          setReplacing(false);
          void run(() => post(`/api/connections/${row.id}/secret`, { secret: value }), "Key replaced.");
        }}>
          <label className="sr-only" htmlFor={`r-${row.id}`}>New key</label>
          <input id={`r-${row.id}`} type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
          <button type="submit" className="button small primary" disabled={!key}>Save key</button>
        </form>
      ) : null}
    </li>
  );
}

export default function ConnectionsPage() {
  const [load, reload] = useLoad<Listing>("/api/connections");
  const [providers] = useLoad<{ providers: ProviderOption[] }>("/api/connections/providers");
  const [adding, setAdding] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const done = (m: string) => {
    setMessage(m);
    setAdding(null);
    reload();
  };

  if (load.state === "loading") return <Loading what="connections" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const options = providers.state === "ready" ? providers.data.providers : [];

  return (
    <section>
      <h1 className="page-title">Connections</h1>
      <p className="lede">Each capability falls back down its list and always ends at manual, where a person does the step.</p>
      {message ? <p className="note" role="status">{message}</p> : null}
      <ul className="caps">
        {load.data.capabilities.map((cap) => {
          const opts = options.filter((o) => o.capability === cap.capability && o.provider !== "manual");
          return (
            <li key={cap.capability} className="cap">
              <div className="cap-head">
                <h2>{cap.capability.replaceAll("_", " ")}</h2>
                {opts.length > 0 ? (
                  <button type="button" className="button small" onClick={() => setAdding(adding === cap.capability ? null : cap.capability)}>
                    {adding === cap.capability ? "Cancel" : "Add"}
                  </button>
                ) : null}
              </div>
              <ol className="conns">
                {cap.connections.map((row) => <Row key={row.id} row={row} onDone={done} />)}
              </ol>
              {adding === cap.capability ? <AddForm capability={cap.capability} options={opts} onDone={done} /> : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
