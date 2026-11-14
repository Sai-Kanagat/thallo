# WP-19: Learning loop and eval harness

Source of truth: `docs/BUILD_PLAN.md`. This file is a self-contained brief for one worker.

- **Wave:** Wave 4. Intelligence
- **Owns (edit only these paths):** `learning/`, `evals/`
- **Needs merged first:** 07, 09
- **Spec sections:** 2.7 learning, modules 20 and 24

## Done when

Edit diffs classified. Rule proposals with scope, provenance and a regression case. Transfer test before promotion. 90-day expiry. Prompt registry. No prompt change deploys on a failed eval run.

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
  thallo/
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
| 20 | Learning loop | Turns each edit and rejection into a tested improvement | Draft versus final diffs, rejection reasons, forecast scores | `rule_proposal` with scope and a regression case, new `prompt_version` | Extended `taste.py`, eval harness | Rules pile up and flatten the voice. Guard: every rule needs its triggering edit, passes old cases, and expires if unused for 90 days | EUR 1 to 2 | 6 | T2 T3 |
| 24 | Eval harness and prompt registry | Proves a prompt or model change is better before it goes live | Regression cases from real edits, golden sets, forecast scores | `prompt_version`, eval runs, pass or fail per change | Test runner, graders | A change that helps one client and hurts another. Guard: per-client case sets, no deploy on a failed run | Under EUR 2 | 4 | T2 T3 T4 |

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
