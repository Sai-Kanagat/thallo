"use client";

// Source capture: the week's three questions, answered by voice or typed, the optional long
// interview, and the shot list. Nothing records or uploads until the consent box is ticked, and
// the server checks that again. Transcripts are untrusted text: rendered as plain text only.

import "./voice.css";

import { useRef, useState } from "react";

import { Failure, Loading } from "@/components/States";
import { ApiError, getSession, post } from "@/lib/api";
import { useLoad } from "@/lib/useLoad";

type Candidate = { id: string; code: string; kind: string; value: string; unit: string | null; state: string };
type Answer = {
  id: string;
  mode: string;
  state: string;
  transcript: string | null;
  detail: string | null;
  candidates: Candidate[];
};
type Question = { id: string; position: number; text: string; answers: Answer[] };
type Shot = { id: string; text: string; people_in_shot: boolean; state: string };
type VoiceWeek = {
  week_start: string;
  questions: Question[];
  interviews: Answer[];
  shots: Shot[];
  consent: { voice: string; people: string };
  can_edit: boolean;
};

async function upload(path: string, blob: Blob): Promise<{ answer_id?: string }> {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    throw new ApiError(0, "offline", "You are offline. Nothing was sent.");
  }
  const s = await getSession();
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": blob.type || "application/octet-stream", "x-csrf-token": s.csrf_token },
    body: blob,
  });
  const body = (await r.json().catch(() => ({}))) as { message?: string; error?: string; answer_id?: string };
  if (!r.ok) throw new ApiError(r.status, body.error ?? "error", body.message ?? "That did not save.");
  return body;
}

function Recorder({ label, target, consentText, onSaved }: {
  label: string;
  target: string;
  consentText: string;
  onSaved: (answerId: string | undefined) => void;
}) {
  const [consent, setConsent] = useState(false);
  const [recording, setRecording] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const rec = useRef<MediaRecorder | null>(null);

  const start = async () => {
    setMessage(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setMessage("The microphone is not available. Type the answer instead.");
      return;
    }
    const chunks: Blob[] = [];
    const r = new MediaRecorder(stream);
    r.ondataavailable = (e) => chunks.push(e.data);
    r.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: r.mimeType.split(";")[0] || "audio/webm" });
      try {
        const out = await upload(`${target}?consent=1`, blob);
        setMessage("Saved. Transcribing now.");
        onSaved(out.answer_id);
      } catch (e) {
        setMessage(e instanceof ApiError ? e.message : "That did not save.");
      }
    };
    rec.current = r;
    r.start();
    setRecording(true);
  };

  const stop = () => {
    rec.current?.stop();
    setRecording(false);
  };

  return (
    <div className="recorder">
      <label className="consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        <span>{consentText}</span>
      </label>
      {recording ? (
        <button type="button" className="button primary" onClick={stop}>Stop and save</button>
      ) : (
        <button type="button" className="button primary" disabled={!consent} onClick={() => void start()}>
          {label}
        </button>
      )}
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </div>
  );
}

function AnswerView({ a, onDone }: { a: Answer; onDone: () => void }) {
  const [text, setText] = useState(a.transcript ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setMessage(null);
    try {
      await fn();
      onDone();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  return (
    <div className="answer">
      <p className="quiet-text">
        {a.mode === "typed" ? "Typed answer" : "Recording"}: {a.state.replace("_", " ")}
      </p>
      {a.state === "pending" || a.state === "failed" ? (
        <button type="button" className="button small" onClick={() => void act(() => post(`/api/voice/answers/${a.id}/transcribe`))}>
          Transcribe
        </button>
      ) : null}
      {a.mode !== "typed" ? (
        <>
          <label className="field">
            <span>{a.state === "done" ? "Transcript (correct it if a name or number is wrong)" : "Type what is said"}</span>
            <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="button" className="button small" disabled={!text.trim()}
                  onClick={() => void act(() => post(`/api/voice/answers/${a.id}/transcript`, { text }))}>
            Save transcript
          </button>
        </>
      ) : (
        <p>{a.transcript}</p>
      )}
      {a.candidates.length ? (
        <div>
          <p className="quiet-text">Numbers to check before any piece can use them:</p>
          <ul className="candidates">
            {a.candidates.map((c) => (
              <li key={c.id}>
                {c.code}: {c.value}{c.unit ? ` ${c.unit}` : ""} ({c.kind}, {c.state}, not verified)
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </div>
  );
}

function Typed({ q, onDone }: { q: Question; onDone: () => void }) {
  const [text, setText] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  return (
    <div>
      <label className="field">
        <span>Or type the answer</span>
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <button type="button" className="button small" disabled={!text.trim()}
              onClick={async () => {
                try {
                  await post(`/api/voice/questions/${q.id}/typed`, { text });
                  setText("");
                  onDone();
                } catch (e) {
                  setMessage(e instanceof ApiError ? e.message : "That did not save.");
                }
              }}>
        Save typed answer
      </button>
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </div>
  );
}

function ShotRow({ s, consentText, onDone }: { s: Shot; consentText: string; onDone: () => void }) {
  const [consent, setConsent] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const send = async (file: File | undefined) => {
    if (!file) return;
    try {
      await upload(`/api/voice/shots/${s.id}/file${consent ? "?consent=1" : ""}`, file);
      onDone();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "That did not save.");
    }
  };
  return (
    <li className="shot">
      <p>{s.text}</p>
      {s.state !== "open" ? (
        <p className="quiet-text">{s.state}</p>
      ) : (
        <>
          {s.people_in_shot ? (
            <label className="consent">
              <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
              <span>{consentText}</span>
            </label>
          ) : null}
          <label className="button small">
            Add photo or clip
            <input className="visually-hidden" type="file" accept="image/*,video/*" capture="environment"
                   disabled={s.people_in_shot && !consent} onChange={(e) => void send(e.target.files?.[0])} />
          </label>
        </>
      )}
      {message ? <p className="quiet-text" role="status">{message}</p> : null}
    </li>
  );
}

export default function VoicePage() {
  const [load, reload] = useLoad<VoiceWeek>("/api/voice");
  const [interview, setInterview] = useState(false);

  if (load.state === "loading") return <Loading what="this week's questions" />;
  if (load.state === "error") return <Failure error={load.error} onRetry={reload} />;
  const w = load.data;

  const saved = async (answerId: string | undefined) => {
    if (answerId) {
      try {
        await post(`/api/voice/answers/${answerId}/transcribe`);
      } catch {
        // the answer is saved; the Transcribe button retries
      }
    }
    reload();
  };

  return (
    <section>
      <h1 className="page-title">Three questions this week</h1>
      <p className="quiet-text">A minute each is plenty. Numbers you mention are checked by a person before use.</p>
      <ol className="questions">
        {w.questions.map((q) => (
          <li key={q.id} className="question">
            <h2>{q.text}</h2>
            {q.answers.map((a) => <AnswerView key={a.id} a={a} onDone={reload} />)}
            <Recorder label="Record answer" target={`/api/voice/questions/${q.id}/recording`}
                      consentText={w.consent.voice} onSaved={(id) => void saved(id)} />
            <Typed q={q} onDone={reload} />
          </li>
        ))}
      </ol>

      <section aria-label="Long interview">
        <h2>Long interview</h2>
        <label className="consent">
          <input type="checkbox" checked={interview} onChange={(e) => setInterview(e.target.checked)} />
          <span>I have time for a longer talk this week (up to an hour)</span>
        </label>
        {interview ? (
          <Recorder label="Start interview" target="/api/voice/interview/recording" consentText={w.consent.voice}
                    onSaved={(id) => void saved(id)} />
        ) : null}
        {w.interviews.map((a) => <AnswerView key={a.id} a={a} onDone={reload} />)}
      </section>

      <section aria-label="Shot list">
        <h2>Shot list</h2>
        <ul className="shots">
          {w.shots.map((s) => <ShotRow key={s.id} s={s} consentText={w.consent.people} onDone={reload} />)}
        </ul>
      </section>
    </section>
  );
}
