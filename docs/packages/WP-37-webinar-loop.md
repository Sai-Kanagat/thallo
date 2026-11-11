# WP-37: Webinar loop

Source of truth: `docs/BUILD_PLAN.md`. This file is a self-contained brief for one worker.

- **Wave:** Wave 7. Add-ons
- **Owns (edit only these paths):** `extensions/webinar/`
- **Needs merged first:** 13, 29
- **Spec sections:** Part 3

## Done when

Promotion, reminders, follow-up and clip briefs for one session a month.

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

# PART 3. Add-ons

Built in the final wave. Rows that are also Part 1 ideas (Business Profile, search pages, customer proof, founder desk, newsletter, subtraction memo, holdouts, claim pack, outcome pricing) are built one wave earlier, in wave 6. Every add-on is switched on per client when its trigger is met.

| Add-on | Idea and value | Trigger |
|---|---|---|
| Business Profile engine | Weekly posts, fresh photos, drafted answers to questions and reviews for local firms. Local search is where walk-in buyers look. | Play A client, API access granted |
| Search and answer-engine pages | Comparison, FAQ and how-to pages from queries with impressions and no page. Answer first, facts in tables. Compounds over months. | Three months of Search Console data |
| Customer-proof engine | After each win, a consent request, three questions, then a quote card and a short case story. The highest-trust content there is. | First three won deals logged |
| Founder profile desk | Drafts for the founder's own LinkedIn from their voice answers, approved on the phone. | Founder commits ten minutes a week |
| Short video from real clips | Captioned vertical cuts of delivery days, demos and expert answers, in the local language. | Shot-list requests answered for four weeks running |
| Newsletter | A fortnightly email assembled from pieces that beat forecast, with replies routed to the lead desk. | 300 consented contacts |
| Webinar loop | Promotion, reminders, follow-up and clips for one session a month. | Play B client with a named host |
| Subtraction memo | A monthly, evidence-backed recommendation of what to stop doing. | Two months of outcome data |
| Share-of-model tracker | Checks whether the brand appears in AI answers for a fixed question set, against named competitors. | Play B client, pages add-on live |
| Pin-code or week holdouts | Withholds ads from a region or a week to measure true lift. | Over EUR 1,500 a month on one platform `[E]` |
| Industry claim pack | Manufacturer-approved specs and wording shared across dealers of one brand. Cuts setup time for the next dealer. | Second dealer of the same make |
| Client CRM sync | One-way push of leads and outcomes to HubSpot, Zoho or a sheet. | Client already lives in a CRM |
| Disclosed site or WhatsApp concierge | An AI-labelled assistant that answers only from the truth library and offers the booking link. | Over 80 verified claims and an L3 record on templates |
| Operator edition | Many brands on one console, white-label reports, one-click handover export. The `PLAN.md` customer. | Three paying clients on the single-brand version |
| Outcome pricing | Price per qualified booking, proven by the ledger. | Six months of baseline for that client |

---
