# WP-03: Truth library

Source of truth: `docs/BUILD_PLAN.md`. This file is a self-contained brief for one worker.

- **Wave:** Wave 1. Foundation
- **Owns (edit only these paths):** `truth/`
- **Needs merged first:** 00
- **Spec sections:** 0.1 code table, 2.7 facts, module 3

## Done when

Typed claims with values and expiry. Slot filling per language. The compile step blocks any digit, superlative or named customer outside a slot. Decimals, Italian and Tamil test cases pass.

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
| 3 | Truth library | Holds every fact the business may state, with proof and an expiry | Site pages, brochures, price lists, transcripts, owner confirmation | `claim` rows: typed value, unit, source span, verified by, expires at | Haiku extraction, human verify screen | A wrong claim poisons everything downstream. A stale price. Guard: only a named person can verify. Prices and offers expire in 30 days by default. Any change voids approvals that used it | Under EUR 1 | 4 | T4 T3 |

---

# Spec: section 0.1

## 0.1 Where the brief, the research and the current positioning are wrong

### The positioning

1. **Kill the "5 marketers to 1" pitch.** The people who hire AI growth operators are marketing leaders. A portfolio piece whose headline is "I replace your team" loses that room. It also describes a buyer I doubt exists in numbers: a 20-person company with five marketers `[?]`. Most owner-led small companies have zero or one.
2. **Sell an operated service, not software.** A founder with 30 minutes a week will not configure claim libraries and design templates. Someone must. That someone is you at first, which is fine: it is the job you want, done in public. Software follows when the workflow stops changing. `PLAN.md` already said this. The brief drifted away from it.
3. **Stop calling it a content system.** Customer zero proves the point. A three-wheeler dealership with walk-in, finance-driven sales is not constrained by LinkedIn carousels. Its constraint is enquiry response, follow-up and finance approval. A reseller of simulation software is constrained by being found during research and by trial follow-up. A system that starts from "fill the calendar" produces the same thing for both. The product must start from the bottleneck and pick the play.

**New one-line position.** For buyers: "Your marketing, run for you. You approve in 30 minutes a week, and every post, lead and euro has a receipt." For hiring managers: "I build and run growth systems that can prove what they did, including the misses."

### The targets

| Target as written | Problem | What this plan does |
|---|---|---|
| Reviewer under 30 min a week | A human must approve every item and must actually look. Fifteen posts read line by line already breaks it. The brief's own routine was 220 min. | Budget the 30 minutes in seconds (section 2.5). Cut volume. Approve bundles. Measure the reviewer's attention. Exclude selling time and say so. |
| Approved without edits rising monthly | Perverse alone. Bland content and a tired reviewer both push it up. | Pair it with outcome per piece and with the reviewer catch rate. Reserve 20% of slots for experiments. |
| Factual errors under 1% at approval | Cannot be shown at this volume by counting posts. Zero errors in 300 units is needed for a 95% bound of 1% `[E: rule of three]`. | Count checkable sentences, not posts. Make numbers impossible for the model to type. Audit 20 sentences by hand weekly. |
| ROAS as high as possible | Wrong objective. The highest ROAS is the first euro on brand search. Maximising it shrinks the business. For lead generation the platform cannot see revenue at all. | Target cost per booked conversation against an allowable figure derived from margin. Feed qualified and won events back to the ad platforms. |
| Cost per lead falling monthly | Falls fastest when lead quality falls. | Track cost per qualified lead, per booked conversation and per won deal, on a 3-month rolling window with intervals. |
| Every channel | Each channel is an API approval, a format set and a failure surface. | Two or three channels per business, chosen by the play. More only when one is saturated. |

### The research documents

- **Agent swarms (both Gemini-style blueprints).** The brief already rejected LangGraph plus CrewAI plus Mem0 plus four guardrail products. I agree and go further: no agent framework at all. Code decides what happens next. Models are typed functions.
- **"Constants to hardcode" in the research pack.** Most are vendor blog numbers (ObserviX, Writer, Zen Media citing others). None go into the product or any pitch. The 61% and 94% buyer figures describe enterprise buying groups and say nothing about a dealership.
- **Bandits, auto-rollback, 5-minute SDR routing, USD 500 a day unreviewed spend.** All rejected by the brief. Correct.
- **Research pack sections 8.2 and 8.6 (automation bias, illusion of control).** The most useful pages in the pack and the least acted on. Nothing in any prior plan measures whether the reviewer is paying attention. This plan does.

### The architecture brief

Good on the ledger, the fail-closed publisher and the honest binomial bounds. Thin or wrong on:

- **No design system.** One line about templates, while "rejected for bad design" is a headline target and you are a designer.
- **Notion as the approval surface.** A Notion status can be flipped by any member or integration, and it cannot bind to a hash. A dealership owner will never open it. Notion stays as a calendar mirror.
- **Standing rules that publish unseen copy.** That contradicts "approval tied to the exact content". My resolution in section 2.8: new copy always gets a human yes. Autonomy is earned on everything around the yes, and the yes is made cheap.
- **A weekly 30-minute interview as the only source.** It alone consumes the whole reviewer budget, and one missed week starves the calendar.
- **HubSpot as the lead surface.** Irrelevant to a business that sells over WhatsApp and a register book.
- **Ads as an add-on.** Two of the eight targets are about ad money.
- **English-only checks.** One pilot business works in Tamil, the other in Italian.

### The existing code (read today)

| File | Issue | Fix |
|---|---|---|
| `cortex_operator/truth.py` | `SENTENCE` splits on every period, so "3.5 tonnes [[C-0001]]." likely becomes two fragments and the first is flagged unsupported. Confirm with a test. | Sentence segmentation that respects decimals and abbreviations. |
| `cortex_operator/truth.py` | `CHECKABLE` lists English superlatives only. | Digits stay universal. Superlative and comparative detection moves to a per-language list plus a model classifier. |
| `cortex_operator/gate.py` | `content_hash` hashes `item.media` as strings. A different image at the same path keeps the approval valid. | Hash file bytes. Store media as content-addressed objects. |
| `cortex_operator/contracts.py` | `Passport` has no expiry, no method, no bundle. | Add `expires_at`, `method`, `bundle_root`. |
| `cortex_operator/taste.py` | Rules are only `Avoid "phrase"` after two removals. Common words will be proposed. No scope, no test. | Section 2.7: typed rules with scope, provenance and a regression case. |
| `cortex_operator/foresight.py` | Point forecasts scored by absolute error. | Interval forecasts, scored against a naive baseline with an interval score. |
| `cortex_operator/store.py` | SQLite. | Postgres with row-level security per tenant. |

### The four inspiration repos

| Repo | What it really is | Take | Improve |
|---|---|---|---|
| EveryInc/Marketing-OS | Markdown skills for strategy, GTM, copy, art direction. No publishing, analytics, approval flow or declared licence. | The learning protocol: a correction stays local to a project until it transfers to fresh work. | Our rules carry a scope and must pass a transfer test before they go brand-wide. Copy nothing, since no licence is declared. |
| Yuzzyuk/marketing-os | One Claude skill, 14 markdown modules, MIT. Slop-pattern catalogue, 15 to 20 variants scored by a panel, a "what I couldn't determine" section. | The slop catalogue, generate-many-then-select, and the honesty section. | It is advice with no enforcement. Ours is enforced in code, and our graders are calibrated against this reviewer's real decisions. |
| efekangokcen/dou-social-crm | A showcase snapshot of an agency CRM. By its own README it does not run. | Encrypted credential vault, role list, activity log. | Its gap is our product: no content approval, no publishing, no measurement. |
| LinkedIn OS gist | A playbook built on third-party figures. | Hook length before "see more", hashtag and mention limits, carousel length, first-hour replies, never automate DMs or connection requests. | Stored as tunable channel rules marked unverified, then tested by our own sealed forecasts. |

---

---

# Spec: section 2.7

## 2.7 Keeping rejection under 10%

Two different failures hide in that target. Bad design is a production problem. Generic copy is a source problem. They get different fixes.

### Design: compile it

- **Tokens.** Colours, type scale, spacing, radii, logo rules and photo treatment live as JSON per brand. Fonts are chosen per script, so Tamil, Italian and English all set properly.
- **Templates.** HTML and CSS, about 8 per format family: single image, carousel, quote, proof card, offer, short video frame. Text flows into slots with length limits. A rotation rule stops the same template appearing twice in five posts.
- **Render.** Headless Chromium produces every size from one master. Carousels export as PDF for LinkedIn and as image sets elsewhere.
- **Real material first.** The photo library is fed by weekly shot-list requests. By default a generated image is a background or an abstract illustration, and people, products and premises are real. A client can switch on generated imagery or video per format through its connections, under the rules in 2.1b.
- **Design lint, in code.** Contrast ratio, minimum font size per channel, text overflow, safe zones, logo clear space, words per slide, face and product crop. A failing render never reaches a person.
- **Reference grader.** A vision model compares each render with the brand's approved and rejected boards and names the nearest rejected example. It flags. It does not block.

### Copy: source it, then strip it

- **Source or silence.** A piece is drafted from the owner's voice answers, staff material and claims. A brief with no source produces no draft.
- **Voice profile.** Per brand and per channel: sentences that pass, sentences that fail, banned terms, formality band, and pairs of on-voice versus off-voice wording.
- **Slop catalogue.** Banned constructions per language: empty openers, false contrasts, triads, hype adjectives, the phrases reviewers have already removed.
- **Many, then one.** Three candidates. Graders score against the voice profile and the brief. The best is selected and the reason is stored.
- **Sameness check.** Each draft is compared with the brand's last 50 pieces and with a small corpus of generic AI marketing copy. Too close to either and it is redrafted.

### Facts: make errors impossible to type

- **Claims as variables.** The writer emits `{{C-0012}}`. Code fills the value and unit from the claim row in the right language. The model never types a number, a price or a customer name.
- **Compile step.** Any digit, superlative, comparison or named customer outside a slot fails the build.
- **Second reader from another model family.** Each remaining assertion is checked against the source spans it was allowed to use. Unsupported sentences are flagged with the span that should have backed them.
- **Weekly hand audit.** The operator verifies 20 checkable sentences against sources. This is the measurement behind T4. Three hundred clean sentences are needed to claim under 1% with 95% confidence `[E]`, which takes roughly 8 to 15 weeks at this volume `[E]`.

### Learning from every rejection

- **One-tap reasons.** Off voice, generic, wrong fact, weak visual, wrong timing, not interesting, other. Each routes to one place: voice rules, slop catalogue, truth library, templates, calendar, idea scoring.
- **Edits become typed rules.** A diff between draft and final is classified: word swap, cut, tone shift, structure change, fact fix. Two similar edits create a `rule_proposal` with a scope (this pillar, this channel, or the whole brand).
- **Each rule ships with its test.** The triggering draft and the human's final version become a regression case. A new prompt version must still pass every old case before it goes live.
- **Transfer before promotion.** A rule stays scoped to its pillar until it has improved fresh work elsewhere. Rules unused for 90 days expire.
- **Graders are graded.** Each week grader verdicts are compared with what the reviewer did. A grader that agrees less than 80% of the time over 30 items loses its vote until recalibrated.
