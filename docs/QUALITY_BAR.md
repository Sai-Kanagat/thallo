# Quality bar (lead, 15 Nov 2026)

Set by Sai: the output and the product must be worth choosing over a human-run marketing agency, end to end. Not only verifiable: well designed, on brand, specific, and free of anything that reads as generated. This file adds to `docs/BUILD_PLAN.md`; where a package brief is silent, this applies. Every package report says how it meets the points that touch it.

## The test
A piece passes when a designer and a copywriter who know the client would sign it as their own work, and the client cannot tell which pieces a person made. The product passes when the owner spends under 30 minutes a week, never has to chase it, and always knows what is waiting on whom.

## 1. Brand fidelity: the brand book is a contract
- Input is whatever the business has: a brand book PDF, a DESIGN.md, a website, a folder of past posts. It becomes one brand contract: tokens (colour, type scale, spacing, grid, radii, logo clear space, photo treatment) plus written rules (voice, do and do not, words never used).
- A person confirms the contract once, on one screen that shows it applied to three sample pieces. Nothing renders for a real tenant from an unconfirmed contract.
- The contract is versioned and hashed. Every asset records the contract version it was made under, so a brand change shows which pieces are stale.
- Conformance is a CODE check: only contract colours, fonts, sizes from the scale, spacing from the grid, logo rules. Off contract is FAIL, not FLAG.
- A weak or missing brand book is said out loud: the system proposes a minimal contract and asks, it never invents an identity silently.
- Owner: new package WP-05b (brand contract intake and conformance), after WP-07.

## 2. Design craft: templates are designed, not generated
- Layout comes from template families built on a real grid and type scale. No image model sets type or composes a layout.
- No template family ships until a human design review of rendered output passes, at every channel size, in every script (Latin, Tamil), with long and short copy, with and without a photo. Sai is that reviewer until someone else is named.
- Each brand keeps a small set of anchor pieces the owner loved. New work is compared against them for consistency and against the week's other pieces for sameness.
- Visual regression: rendered snapshots per template and brand are kept; a change in pixels needs an approved reason.
- A design slop catalogue, the twin of the copy one: default gradients, generic rounded buttons and pills, stock-photo look, icon soup, centred everything, fake UI, decorative emoji, lorem-shaped filler. Maintained from real rejections.
- Owner: WP-07 (visual layer), WP-05 follow-up (template review and snapshots).

## 3. Imagery
- Default sources are the client's real photos and template renders. Generated images are off in v1.
- Client photos get technical checks (resolution, blur, exposure, crop inside safe zones, faces not cut) and a human sign-off. A missing photo produces a shot-list request, never a stand-in.
- If generation is ever enabled: provenance recorded, an artefact check (hands, faces, garbled text, warped logos and products), and a FLAG the approver must acknowledge on every use. Never a generated person presented as a customer, never a generated product.
- A vision grader looks at every finished image. It can only FLAG.
- Owner: WP-07, WP-10 (shot lists), WP-11 (media connections).

## 4. Copy
- Voice comes from the client's own words: transcripts and past writing, with provenance. Rules without a source are not rules.
- Specificity rule: a piece must carry at least one concrete detail from this client's source (a name, a number bound to a claim, a place, a customer question). No source, no slot.
- Slop catalogue per language, maintained from real edits. Tamil and Italian are reviewed by a native reader before any pilot piece ships.
- "Approved without edits" is never read alone: it sits beside outcome per piece, so blandness cannot win.
- Owner: WP-06 (exists), WP-07, WP-19.

## 5. Strategy
- Every piece names its play, pillar, funnel stage and the one metric it is meant to move. A piece that cannot say why it exists is not planned.
- The weekly plan shows its reasoning in two lines, and what it chose not to do.
- Owner: WP-15, WP-17.

## 6. The product itself
- The web app follows `~/.claude/DESIGN.md` (or the project's own). No default component-library look. Every screen has designed empty, loading, error and offline states.
- Before WP-09 or any UI package merges: a design review of the running app with screenshots, a keyboard and screen-reader pass, and a phone-width pass, in addition to the security review.
- Words in the UI are plain and specific. A refusal always names the check and the next step.
- Speed: the review deck opens and a card is approvable inside two seconds on a phone.
- Owner: WP-09 and every later UI package.

## 7. Seamless end to end
- One weekly rhythm: three voice questions, a plan, a review deck. Nothing asks the owner the same thing twice.
- Every waiting state names who it waits on. Every failure falls to the manual floor and says so.
- Onboarding to first approved piece in one sitting, with the brand contract confirmed in it.
- A weekly memo in plain words: what went out, what it did, what changes next week, what it cost.
- Owner: WP-09, WP-10, WP-21, WP-26.

## 8. Proof that the bar is met
- Eval harness (WP-19) holds golden sets per client: copy, images and plans. No prompt, model or template change goes live on a failed run.
- Blind comparison before the first pilot ships: a set of Thallo pieces beside human-agency pieces for the same brief, scored by people who do not know which is which, on brand fit, craft, specificity and would-you-post-it. Thallo ships to a client when it is at least level.
- Tracked weekly per client: first-pass approval rate, edit distance, flags per piece, minutes of owner time, outcome per piece.
- Pilot entry bar: twenty consecutive pieces Sai would approve with no substantive edit, across both pilots' formats.

## What this costs
Two new pieces of work beyond the 42 packages: WP-05b (brand contract intake and conformance) and the blind comparison. The visual layer enlarges WP-07. Design review becomes a merge gate for UI packages. Nothing here publishes, sends or spends.

## 9. Strategy and proof come before breadth (added 15 Nov)
An agency earns its fee by deciding what to post and showing it worked. So after the core loop (WP-07 checks, WP-08 publisher, WP-09 app, WP-10 capture), the order is:
1. WP-15 growth model and plays: every slot has a play, pillar, stage and target metric.
2. WP-17 planner: the weekly plan with its reasoning and what it chose not to do.
3. WP-12 link and touch tracking, then WP-18 measurement: outcome per piece.
4. WP-21 scoreboard and weekly memo: what went out, what it did, what it cost, what changes.
5. WP-19 learning loop and eval harness, with the blind comparison.
Only then the extension packages (ads, video, pages, inbox, and the rest).
Standing rules: approval rate is never shown without outcome per piece beside it. Sai is the design reviewer of record for every template family and every UI package until he names someone else; no checker replaces that sign-off.

## 10. Outside tools: the intake rule
Thallo is not a bundle of repos. A tool enters only when a pilot needs the capability, and then in the weakest form that works:
1. Idea only: read it, port the pattern or the checklist into our own code with tests. Default.
2. Seed data: use its lists (slop phrases, design tells, audit checks) as starting catalogues, attributed, licence checked.
3. Provider behind the registry: only for a maintained project with a clear licence, a pinned version, no secret reaching it except through the vault, and a manual fallback.
Never: anything that generates people or products, evades bot detection, scrapes personal data for outreach, warms up or rotates mailboxes, sends without a human, or spends without Sai's yes.

Triage of the 15 Nov list (from Sai's saved posts; repo facts NOT yet verified, a researcher pass must confirm each exists, its licence and its upkeep before any use):
- Adopt as pattern, soon: HyperFrames (HTML to MP4 fits our template renderer, WP-36); Humanizer, Impeccable, Hallmark, taste-skill (seed the copy and design slop catalogues, WP-07, and the UI review gate, WP-09); C2PA provenance, through the official C2PA tooling, to label our renders and to detect AI-made incoming client photos (WP-07, WP-11); a PII and injection screen as a plain code check (WP-07); the "needs check" state for ambiguous sends (already in WP-08).
- Adopt as pattern, in its package: claude-ads audit checks, audit only (WP-22); AEO and GEO page checks, sitemap and JSON-LD validation (WP-27); share-of-model tracking (WP-38); "AI researches, human sends" lead research (WP-13); brand memory that compounds (WP-19); pre-publish compliance checks (WP-07); cookieless analytics (WP-12); revenue attribution for the Won column (WP-18, WP-35); last30days style signal intake and Firecrawl for public pages that allow it (WP-16); Wikidata or web checks that can only FLAG (WP-33).
- Rejected: Open-Generative-AI and any uncensored image or video generation (generated imagery is off in v1 and this one ships with no content filters); Camofox and any stealth or anti-bot browser; Browser-Use for logging into client accounts; Lead Orchestra and scraped lead lists; Emareach, Warmbly and any mailbox warm-up or rotation; programmatic SEO at scale (mass thin pages are slop by definition); HyperFX MCP for executing ads; extra publishers (Postflare, postiz-cheap) until one publisher is proven; NeMo Guardrails (no conversational agent exists); MarketingAgent; MetricUI (a stock component look is what section 6 forbids).
