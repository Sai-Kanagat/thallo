# Cortex Operator: a marketing department for one-person marketers

## Context

Sai wants a full-stack AI marketing system that serves four goals at once: his portfolio, his LinkedIn story, proof of skill for AI growth operator roles, and a product he can sell. It is not built around e-FEM or any one industry. e-FEM is at most a stepping stone. Two isolated brainstorm rounds produced this direction. Existing assets: Cortex (13-agent LangGraph crew, image QA, human approval pause, cost metering, evals) and the Agency OS approval gate.

## The thesis

Every AI marketing tool can write. Cortex knows three things they do not, with receipts:

1. **Truth:** what can be said. Claim Ledger with evidence; factual sentences without a source cannot compile; claims decay unless reconfirmed.
2. **Taste:** what you would approve. Repeated edits and rejections become proposed rules, accepted like code changes. A graveyard records every killed idea and why.
3. **Foresight:** what will happen. Sealed, hash-chained forecasts for every suggestion, scored against a naive baseline, including every miss.

Target claim, to be proven: supervising the system takes measurably less time every week.

## Customer

**Primary:** independent operators (fractional CMOs, boutique agencies, executive ghostwriters) running 3 to 10 brands in any industry. They own strategy and lack execution hands. One sale brings many brands, and Sai is becoming this person.
**Later:** founders with no marketer, through an operator or self-serve.
**Design partners:** Sai's own brand first, then any consenting company. e-FEM is one option.

Market evidence is mostly vendor content, so validate with 10 operator conversations.

## The product

- **Brand Memory:** open, versioned folder per brand: voice, claims and evidence, graveyard, hooks and results. The client owns it; Notion is its view. Portability is the trust wedge.
- **Notion cockpit:** rows, notes and comments are picked up within 15 minutes, through a per-brand schema map.
- **Weekly rhythm for a Monday post:** ideas proposed the Friday before; generated the Monday before; approved by Wednesday; Thursday noon unapproved items go on hold; resized and scheduled by Friday; metrics return at T+2 and T+7.
- **Approval Passport:** approver, time, content hash, rule version, claim IDs, QA result. Any edit voids approval.
- **Asset factory:** one master design rendered to every size from templates; AI images where suitable; human visuals drop in.
- **Publishers:** scheduler first (Postiz or Metricool), then direct adapters; WordPress, newsletter, YouTube. Ads are built and launched paused, with spend caps in code.
- **Channels:** SEO, blog, website, company and CEO LinkedIn, Instagram, TikTok, YouTube, Google and YouTube ads, newsletter, AI search visibility. Enabled per brand.
- **Operator console:** all brands, approvals, edit minutes, cost, forecast score.

## Posting layer

- **Publish queue:** one per brand, behind the gate. Only hash-approved items enter.
- **Pre-flight checks per channel:** size, length, format, link, time zone, account connected. Failures reach the operator before Friday, not on the day.
- **Reliable delivery:** idempotent jobs so nothing posts twice, retries with backoff, and an alert plus a one-tap reschedule when a post fails.
- **Confirmation:** the live link and status return to Notion, or the item is flagged if it never appears.
- **Controls:** per-brand and global kill switch, pause all scheduled items, and a pull-back path for scheduled posts.
- **Adapters:** scheduler first, then direct APIs, each behind the same interface. Channel-specific extras such as first comments, alt text and hashtags where supported.

## Analytics layer

- **Collection:** connectors for GA4, Search Console, YouTube, LinkedIn, Instagram and Facebook, TikTok, Google Ads, newsletter, scheduler and CRM, each enabled per brand. Metric availability differs per platform, checked per adapter.
- **One data model:** every metric normalised in Postgres against brand, campaign, post, channel and date, with raw pulls kept for audit.
- **Tracking:** UTMs generated automatically at campaign level, naming rules enforced, consent respected, no pixels in email.
- **Measures:**
  - Content: reach, engagement, clicks, best hooks and formats.
  - Channel: output against result, cost per outcome.
  - SEO: rankings, queries, AI search citations.
  - Ads: spend, CPC, conversions, return, against caps.
  - Funnel: visit to lead to meeting to deal.
  - System: edit minutes, approval rate, on-time rate, cost per post, forecast score.
- **Outputs:** metrics written back to Notion; operator console across all brands; white-label client dashboard; weekly summary and monthly report; alerts for drops, spikes and spend anomalies.
- **Analyst agent:** explains what changed, and feeds the Strategist, forecasts and Subtraction Agent, so results shape next week's calendar.
- **Honesty:** attribution is labelled directional, with gaps from consent or missing API data shown, and low sample sizes marked.

## Signature features

- **Counterfactual Quarter:** paste a URL. Public sources only, dated claims, apparent inconsistencies, a ghost plan built only from pre-quarter evidence, one finished week on click. Labelled simulation, no uplift claimed, refuses on thin evidence. Private expiring links, quotas, spend kill switch.
- **Ghost-run applications:** a private, hand-checked run sent individually to a hiring lead as an unsolicited simulation, with a few a week at most, no tracking, and a delete link.
- **Forecast record:** operator forecasts first (approval, edit distance, cost), then audience forecasts. Weekly story is the biggest miss with its receipt. Human vs Machine scoreboard.
- **Subtraction Agent:** monthly recommendation of what to stop, with evidence.
- **Sameness detector:** flags when a brand's output recycles itself.
- **Dissent Engine:** interviews the CEO, drafts only views they really hold. Includes a weekly voice call, web voice first.
- **Taste Diff Report:** weekly proof of value operators forward to clients.
- **Client Handover Export:** one click, no hostages.

Industry packs later: Proof Engine (real work as a recurring public show), Champion's Dossier (internal business case), regulated claims pack.

Rejected: self-serve SaaS first, rival-tool benchmark now, cross-company networks, consortia, fake personas or engagement, any avatar without explicit consent and disclosure.

## Commercial path

1. **Service first:** Brand Memory Audit EUR 400 to 600, four-week pilot EUR 900 to 1,200, then EUR 500 to 800 per brand per month, white-labelled for operators. All untested.
2. **Licensed kit** after 2 to 3 brands: EUR 1,500 to 2,500 plus about EUR 99 per brand per month.
3. **Software** once the workflow stops changing.

## How each goal is served

| Goal | Artifact |
|---|---|
| Portfolio | Counterfactual demo, operator console, architecture and eval write-up |
| LinkedIn | Weekly biggest miss with receipt, agents learning his veto |
| Job pitch | Ghost-run application, forecast page link |
| Sellable product | Audit, pilot, kit, then software |

## Matteo

Pitch as one design partner after it runs on Sai's brand. Value: real calendar, real reviewers, real edit-time data, an existing hub. Ask: free pilot in exchange for written OK that Sai owns the engine and can publish anonymised numbers. Any consenting company can replace this.

## Build order

0. Kill tests, before committing: edit-to-rules on 20 to 30 pairs; claim extraction at about 90% hand-checked precision on Sai's site and four private domains; mock forecast page shown to 5 target readers; 10 operator conversations.
1. Engine: Brand Memory, Notion adapter, state machine, gate, ledgers. Proof: fake-clock week, zero external sends.
2. Sai's brand end to end on LinkedIn, including the posting layer and the first analytics connectors (GA4, Search Console, LinkedIn). Proof: one real approved post, confirmed live, forecast sealed and scored, metrics back in Notion.
3. Counterfactual Quarter and first ghost runs.
4. First paid audit or pilot with one operator, with the weekly client report.
5. Asset factory, then remaining channels and their connectors, then ads builder and Google Ads analytics.
6. Operator console, white-label dashboard, handover export, kit.

Reuse: `cortex-studio/apps/api/app/graph/agents.py` (`resilient`, `_gen_and_qa`, `human_gate`, `critic`), `app/llm`, `app/memory`, `app/tools/registry.py`; `~/agency-os/gate/app/gate.py`.

## How it gets built

**Repo:** new private repo `~/cortex-operator`, because `cortex-studio` is public MIT and this is meant to be sold. Copy what is reused. `cortex-studio` stays as the public showcase. The Brand Memory format can be opened later as a deliberate choice. Brand data never enters the engine repo.

**Stack:** Python 3.12, FastAPI, LangGraph, Pydantic, Postgres, a worker for scheduled jobs, Next.js console, Docker Compose. Mock mode for every outside service, so everything runs and tests with no keys.

**Execution mode:** Sai turns on bypass permissions. One orchestrator session owns contracts, merges and the final check. Subagents build in parallel, each in its own git worktree on its own branch.

1. **Contracts first, by the orchestrator:** schemas, interfaces, event names, fake clock, mock adapters, CI. Nothing fans out until these are merged, so parallel work cannot drift.
2. **Parallel wave one:**
   - A: Brand Memory and the Truth, Taste and Foresight ledgers.
   - B: Notion adapter and content state machine.
   - C: gate, publish queue, posting adapters.
   - D: analytics model and connectors.
   - E: agent crew, ported from Cortex.
   - F: Counterfactual Quarter.
3. **Parallel wave two:** asset factory, operator console, reports, remaining channels, ads builder.
4. **Every branch merges only if:** tests pass, a separate review agent finds no blocking bug, and a security review passes. The verifier agent runs the fake-clock week after each merge wave.
5. **Reporting:** outcomes only, with a plain list of what is real, what is mocked and what is waiting on Sai.

**App:** MonoCode v0.16.0 (installed) as the build cockpit, using its orchestrator, worktrees and write scopes. First run one throwaway task through a Claude lead and a Codex worker. If orchestration misbehaves, fall back to Claude Code subagents with `/codex` for review. Stay on the production app, not the Native preview.

**Models and effort (hard ceiling: Opus 5.5 at medium; lowest usage for the highest result):** lead, contracts, gate, ledgers: Opus 5.5 at medium. State machine, agent crew, posting, analytics, Counterfactual Quarter: Sonnet 5.5 at medium. Connectors, console UI, templates, reports: Codex at medium on Sai's ChatGPT subscription. Fixtures, docs, mechanical changes: Haiku 5.5 at low.

**Usage rules:** build only what the first real post needs until the kill tests pass. At most 3 workers at once, with short briefs and narrow file scopes. Port Cortex and the gate rather than regenerating. Deterministic code for rules, sizing and scoring. Mocks for all test runs. Cross-model review only on gate, ledgers and posting; automated tests cover the rest. This overrides the wider parallel waves and per-branch review rule below where they conflict: wave one becomes A, B and C first, then D, E and F.

**Rules for every agent:** before any worker starts, the repo gets an `AGENTS.md` carrying the same hard limits and commit rules as `CLAUDE.md`, because Codex does not read Sai's Claude config. Confirm each CLI is logged in before the first run.

**Tools allowed:** any skill, connector or subagent that helps, including context7 for current API docs, web research, browser testing, design skills for the console, and the critic before anything outward-facing.

**Bypass does not change these limits:** no publishing, sending or outreach without Sai's explicit OK; no purchases; no accounts created or passwords entered; no tokens pasted in chat, so secrets go in `.env` by Sai; no secrets committed, with the gitleaks hook kept on; no deletion of existing work; only sandbox data until a real run is approved. Commits as Sai with no Claude trailers, per his GitHub rule.

**Sai's manual list, batched at the end:** Notion integration token, scheduler account, Google and LinkedIn connections, model keys, any spend approval, the 5 reader tests and 10 operator conversations.

**Handoff to MonoCode (first action after approval, nothing else is built here):**
1. Create `~/cortex-operator` as a local git repo, with a private GitHub remote only if `gh` is already logged in.
2. Add `PLAN.md` (this plan), `AGENTS.md` (rulebook for all agents), `CLAUDE.md` (points to `AGENTS.md`), `HANDOFF.md` (kickoff prompt), `.gitignore`, `.env.example`, and the gitleaks pre-commit hook from `~/agency-os`.
3. `AGENTS.md` states: follow `PLAN.md`; stay in assigned scope; never publish, send, spend, create accounts or handle passwords and tokens; secrets only in `.env`; hook stays on; mocks and sandbox data; commit as Sai with no AI trailers; no em dashes or emoji in copy; never fabricate data.
4. Save the pending memory notes, then open the folder in MonoCode. Sai starts a Claude Code session at Opus 5.5 medium and pastes the kickoff prompt.

## Value scenarios (arithmetic on assumptions, not forecasts)

| Case | Assumption | Result |
|---|---|---|
| Code alone, no customers | Pre-revenue | Little sale value |
| Likely good outcome | 5 to 10 brands at EUR 500 to 800 | EUR 2,500 to 8,000 a month |
| Best-case software | 300 operators, 5 brands each, EUR 99 per brand | About EUR 1.8M ARR |
| Best-case sale | 3 to 6x ARR, multiple unverified | About EUR 5M to 11M |

Most likely real payoff: the job or client work it unlocks.

## Running modes (estimates, confirm before purchase)

Lean about EUR 15/month on free tiers and templates. Pro about EUR 50-100/month with stronger models, images, VPS and scheduler; a paying client funds this.

## Risks

- Taste rules may not cut review time: measured from week 1, judged by week 4.
- False inconsistency findings could harm credibility or be defamatory: precision gate, neutral wording, no public gallery, human check before anything leaves.
- Forecast page may prove honesty, not growth: tested with 5 readers.
- Unsolicited ghost runs: check GDPR and Italian contact rules first.
- Runway is short, so service income comes before software.
- Focus: park Agency OS explicitly. Scope grows fast, so the kill tests decide.
- Platform rules are from third-party sources; check official docs and scheduler terms per adapter.
- Invoicing starts with the first client, which is when the permit question gets settled.

## Verification

- Tests on the gate and ledgers: missing approval, changed hash, missing evidence, broken chain, spend cap all block.
- Fake-clock week including a missed approval.
- Time-cut leakage test on Counterfactual Quarter.
- Real proof: one post live with forecast scored, one hand-validated report, edit minutes tracked over 4 weeks.
