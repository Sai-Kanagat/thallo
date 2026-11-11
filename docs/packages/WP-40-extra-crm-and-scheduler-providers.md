# WP-40: Extra CRM and scheduler providers

Source of truth: `docs/BUILD_PLAN.md`. This file is a self-contained brief for one worker.

- **Wave:** Wave 7. Add-ons
- **Owns (edit only these paths):** `providers/crm/`, `providers/publishing/`
- **Needs merged first:** 14, 08
- **Spec sections:** 2.1b

## Done when

Further providers, each passing the contract tests.

## Report back

What is real, what is mocked, what needs Sai, and what you could not verify.

---

# Rules for every package

### 4.1 Rules for the build

- **Contracts first.** Package WP-00 freezes schemas, state names, the connection contract, event names and the fake clock. Nothing fans out before it is merged. After that, a contract change goes through the lead only.
- **One writer per path.** Each package owns the paths listed for it. A worker never edits outside them.
- **Mocks everywhere.** Every connection has `mock()`. All tests run with no keys and no network. No real send, post or spend without Sai's explicit yes.
- **Port what exists.** Section 4.2 lists what to keep. Do not regenerate it.
- **Deterministic code for rules.** Hashing, scoring, sizing, routing, caps and unlock rules are plain code with tests. Models are called only where language or judgment is needed.
- **Manual mode ships first** for every capability, then the default provider, then the alternatives.
- **Every package ends with:** tests passing, a short note of what is real, what is mocked and what needs Sai.
- **Merge gate.** Tests pass, a separate review agent finds no blocking bug, and a security review passes on the gate, vault, publisher, messaging and API packages. After each wave the verifier runs the fake-clock week end to end.
- **Parallelism.** Packages inside a wave are independent and can run together, three workers at a time by default. Raise that only if reviews keep up.
- **Models.** Lead, contracts, gate, truth, trust: Opus 5.5. Most packages: Sonnet 5.5. Fixtures, seed data, docs: Haiku 5.5. A second model family reviews the gate, truth and publisher.
- **Orchestration tool.** The packages are tool-agnostic. In Claude Code: one lead session, one git worktree per package. In MonoCode: one card per package, pasting the package row and the sections it names. `HANDOFF.md` records that MonoCode failed on its last assignment card, so test it on WP-01 before trusting it with a wave.
- **Existing hard limits stay.** `AGENTS.md` applies to every agent: no publishing, spending, account creation or secrets in chat. No AI attribution in commits. No em dashes or emoji in copy.
- **Apply for platform access on day one** (WhatsApp Cloud, Google Ads, Business Profile, any direct social APIs). Approval takes time nobody controls, and manual mode covers the gap.

### 4.3 Repository layout

```
cortex-operator/
  cortex_operator/
    contracts/     shared types, states, events, fake clock
    store/         Postgres access, tenant scoping
    connections/   registry, contract, vault
    providers/     one folder per capability, one file per provider
    truth/  gate/  brand/  media/  copy/  checks/  publish/
    capture/  tracking/  leads/  nurture/  growth/
    signals/  planner/  measure/  learning/  trust/  reports/
    ads/  engage/  extensions/
    api/           FastAPI routes and the MCP server
    worker/        job queue and state machine runner
  db/migrations/
  web/             Next.js phone-first app
  brand_templates/ HTML and CSS templates, tokens
  evals/           regression cases and golden sets
  demo/            seeded fictional business
  tests/
  docs/            this plan, one file per work package
```

---

# Spec: section 2.1b

## 2.1b Connections: every outside capability is a swappable port

The rule: the core never calls a vendor. It calls a **capability**. Each client picks which **connection** serves that capability, and can change it at any time, per brand, per format, or for one single piece.

**Three modes, always in this order of availability.**

1. **Manual.** A person uploads the file, pastes the link, or types the number. Every capability has this mode, so the product works with zero connections and no refused API can block a client.
2. **Local.** Runs on your own machine or server: a template render, a local model, a self-hosted tool.
3. **Hosted.** A paid provider, with a model picker where the provider offers several.

**What never changes when a connection changes.** The gate, content hashing, claim checks, provenance, disclosure labels, cost ledger and regional policy all sit above the port. Swapping Postiz for a direct API, or one video model for another, cannot route around an approval.

| Capability | Manual | Local | Hosted, examples | Default |
|---|---|---|---|---|
| Text: plan, draft, grade, second read | Paste your own copy | Any local model behind an OpenAI-compatible endpoint (Ollama and similar) | Claude, Gemini, OpenAI, any OpenAI-compatible provider. Model chosen per role. | Claude tiers, Gemini as second reader |
| Image | Upload a photo or finished graphic | Template render. A local diffusion endpoint. | Gemini image models, models listed on Muapi, others | Real photos, template render, hosted model for backgrounds |
| Video | Upload a finished clip | Template render to video (HyperFrames or Remotion `[?: licence]`), `ffmpeg` cuts and captions, raea on a local GPU | Any generative video model reachable through a provider such as Muapi or Gemini `[?: prices]` | Local cuts of real clips |
| Transcription | Type the answer | Local Whisper-class model | Hosted transcription | Local |
| Voiceover | Record your own | Local speech model | Hosted speech model | Record your own |
| Publishing | Copy pack, then confirm the live link | Self-hosted Postiz | Postiz hosted, direct platform APIs, another scheduler | Postiz hosted |
| Calendar view | Built-in week plan | None | Notion, Google Calendar | Built-in, mirrored to Notion |
| Leads and CRM | Built-in lead desk, CSV in and out | None | HubSpot, Zoho, a Google Sheet | Built-in |
| Messaging | Click-to-chat links, the seller's own mailbox | Your own SMTP server | WhatsApp Cloud API, Brevo, Resend, SES | Manual for first replies, always |
| Booking | Callback slot list | None | Cal.com, Calendly | Cal.com |
| Analytics in | CSV upload or typed numbers | Our own redirect and code logs | GA4, Search Console, scheduler analytics, Plausible | Own logs plus GA4 and Search Console |
| Ads | Person makes the change, spend imported by CSV | None | Meta, Google Ads, LinkedIn Ads | Manual |
| Research | Paste notes | None | A web search provider | Off |
| Storage | None | Local disk | Supabase storage, any S3-compatible store | Supabase |
| Notifications | None | None | Email, WhatsApp, Telegram, web push | Email and push |
| Automation hooks | None | None | Outbound webhooks to n8n, Zapier or Make. Events out only. They cannot publish. | Off |

**The connection contract.** Every provider, including manual ones, implements the same five calls:

- `describe()`: what it can do, limits, models offered, unit cost, licence note.
- `health()`: connected, token expiry, quota left.
- `estimate(request)`: cost and time before anything runs.
- `run(request)`: returns the result with provenance (who or what made it, which model, which settings), cost and external ids.
- `mock()`: a deterministic fake for tests and the demo client.

**Selection.** Each client has a default connection per capability and an ordered fallback list. A brief, a template or a single review card can override it: "use my upload", "render locally", "regenerate with model X". The choice and its cost are stored on the media or variant row.

**Rules for generated media, whatever the connection.**

- Provenance is recorded per file: client photo, client clip, template render, or generated with model and prompt.
- Generated people, products and premises are off by default. A client can switch them on per format.
- A real, identifiable person is never generated, voiced or lip-synced without a consent record on file.
- Generated or materially altered media carries the disclosure label the regional policy row requires.
- A generated image never carries a factual claim about the product. Claims ride on real photos or typeset text.

**Adding a provider** is one file that implements the contract plus a row in the registry. No core code changes.
