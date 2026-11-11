# WP-04: Gate, approvals and audit log

Source of truth: `docs/BUILD_PLAN.md`. This file is a self-contained brief for one worker.

- **Wave:** Wave 1. Foundation
- **Owns (edit only these paths):** `gate/`, `trust/audit.py`
- **Needs merged first:** 00
- **Spec sections:** 2.8, module 11, 5 revision 4

## Done when

Bundle root hash over text, media bytes, claim evidence, rule version and channel. Canonical form documented. Approval expiry. Canary refusal. Hash-chained audit log. All block cases in Verification pass.

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

# Module rows

| # | Module | Job in plain language | Inputs | Outputs | Tools | Failure modes, and the guard | Cost | Effort | Moves |
|---|---|---|---|---|---|---|---|---|---|
| 5 | The engine's job in the first quarter is measurement hygiene: every lead with a source, every outcome logged, every number with its sample size. The scoreboard prints "no decision" proudly. Optimisation claims wait until the data exists. |
| 11 | Review deck and approval | A person approves exactly what will go out | Bundle of variants and media, claims used, check results, forecast | `approval` bound to the bundle root hash, with approver, method and expiry | PWA, existing `gate.py` extended | Edit after approval, stale facts, rubber-stamping. Guard: any byte or claim change voids the approval, approvals expire, canaries measure attention | Hosting | 6 | T1 T3 T4 |

---

# Revision 4

| 4 | A written canonical form for hashing: normalised whitespace, resolved links, media hashed before upload. Readback compares canonical text and reports image changes as a warning. A typo path re-approves only the diff, in one tap. |

---

# Spec: section 2.8

## 2.8 Where a human always stays, and the trust ladder

### Permanent human decisions

| Stays human | Why |
|---|---|
| The yes on any new copy or creative | The hard rule. Also the only real check on taste. |
| Verifying a claim | Every downstream check trusts this table. If a model can write to it, nothing is proven. |
| First reply to a named person, and all negotiation | Cannot be recalled. A wrong promise on price, finance or delivery is a liability. |
| Replies to comments, complaints and reviews | Platform rules and plain decency. Suggestions are fine. |
| Any spend increase, new campaign or new audience | Money moved in error does not come back. |
| Statements about named customers, competitors, pricing, legal or safety matters | Highest liability per sentence. The facts live in contracts and heads. |
| Raising a trust level or signing a template | Accountability needs a name. |
| Strategy: who to serve, what to stand for, what to refuse | The system has no stake and no customers. |
| Consent calls and anything the region policy marks as legal | Out of scope for software. |

### The ladder

Autonomy is tracked per client and per task type, for example "LinkedIn text post" or "offer carousel". A defect is a rejection or an edit that changes meaning, a claim or anything a hard rule covers. Typo fixes are logged and do not count.

| Level | What the system does alone | What the person sees | To unlock | 95% upper bound on true defect rate |
|---|---|---|---|---|
| L0 Shadow | Drafts only | The draft beside their own version | Starting state for a new task type | n/a |
| L1 Full review | Drafts, checks, renders | Every item as a full card with every check open | Calibration batch of 20 done, voice rules v1 signed | n/a |
| L2 Bundle review | Groups variants, sorts, collapses clean checks | One card per bundle, flags open | 50 items, at most 2 defects, none escaped, no hard-rule hit | 12.1% |
| L3 Express list | Presents clean bundles as one list with "approve all except". Auto-accepts plan slots for proven pillars. Picks posting times. | Thumbnails and first lines. Any item opens in full. | 100 items, at most 1 defect, none escaped, canary catch rate of at least 80% | 4.7% |
| L4 Template instances | Sends or posts instances of a hash-approved template whose slots fill only from verified data | A 1-in-20 sample after the fact, plus every flagged instance | Template signed, 200 instances, at most 3 defects, none escaped | 3.8% |

Bounds are exact one-sided binomial figures, taken from the brief and rechecked `[E]`.

**What this ladder refuses to do.** New copy never ships on a standing rule. At L3 the person still approves exact hashes, in one tap, after seeing them. L4 covers only text a person already approved word for word: an acknowledgement, a nurture email, a reminder. That is how "approval tied to the exact content" and "under 30 minutes" both hold.

**Reviewer canaries.** From L2 on, about 1 item in 20 is a planted flaw: a claim swapped for a wrong one, an off-brand image, a banned phrase. It is marked internally and the publisher refuses it whatever happens. If the reviewer approves it, they are told at once and that task type drops a level. The catch rate is the first honest measure of the human gate. Five canaries is thin evidence, and the screen says so.

**Demotion, applied the same day.**

- A hard-rule hit that reached approval, or any escaped defect: back to L1, incident note opened.
- Two defects in the last 20 items: down one level.
- A canary approved: down one level.
- Model or prompt version changes: down one level, re-earned with 30 clean items.
- A region policy row changes: affected task types to L1.
- A cost breaker trips twice in a week: that task type pauses.
