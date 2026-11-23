# Wave 8: closing the loop (lead, 23 Nov 2026)

Source: Sai's review of the ReStructure AI Europe blueprints (sales and marketing team, local marketing, business OS). Their loop runs from market signal to money in the bank. Ours stopped at a booking. This wave closes that gap without giving up the fact gate or the human on every claim. Whiteboard: https://claude.ai/artifact/7dPm7a4Mu8SG4LQZrNBsC4

Rules that do not change: nothing sends, posts or spends without the trust ladder allowing it or a human approving it; secrets only from the vault; untrusted text only in `untrusted_data`; tenant RLS everywhere; consent bound to recipient and channel; every outbound AI conversation says it is AI (EU AI Act Art. 50, from Aug 2026).

## New packages

| ID | Package | Owns | Needs | Done when |
|---|---|---|---|---|
| WP-42 | Quotes and deposits | `sales/quotes/`, `providers/payments/` | 03, 13, 14 | A quote is drafted only from verified price claims, shown to the owner, approved on the exact version, then sent with a deposit link created on the client's own Stripe account (Stripe Connect or the client's restricted key from the vault). Thallo never holds money. Payment webhooks mark the deal won and feed WP-18. Expired quotes, refunds and disputes go to the owner. Manual floor: a PDF quote and the owner's own payment link. |
| WP-43 | Market signals | `signals/market/`, `providers/research/` | 16 | Trend spikes (Google Trends, pytrends or SerpApi behind the registry) and competitor ads from public ad libraries (Meta Ad Library API, Google Ads Transparency) arrive as dated signals. Signals can only propose plan slots; they never become claims. Reviews of the business feed the customer picture (WP-28 consent rules apply). |
| WP-44 | Call tracking and missed-call text-back | `tracking/calls/`, `providers/telephony/` | 12, 13 | A tracking number per channel (Twilio, or the client's existing CallRail) attributes calls. A missed call gets one AI-labelled text within a minute offering the booking link, only where the caller's number may lawfully be texted; everything else waits for the owner. No call recording without explicit consent. |
| WP-45 | Earned auto-replies | `engage/auto/` | 20, 23, 39 | Extends WP-23. A reply type (FAQ answer from verified claims, booking link, thanks) can run unattended only after the trust ladder unlocks it for that client and channel: N clean approved replies, zero edits, canary sample kept. Replies are labelled as AI. Prices, complaints, health, legal and anything without a verified source always go to a human. Pulls WP-39 (disclosed concierge) forward. |
| WP-46 | Generated imagery, gated | `media/generate/` | 07, 11 | See "Image generation" below. Off by default per client. |
| WP-47 | Morning page | `reports/morning/` | 18, 21 | One page each morning, not a dashboard: what went out and what it did, cost per customer by channel, what waits on the owner and on whom, and money leaking (ad sets with spend and no leads, unanswered leads, expired quotes). Nightly job computes cost per customer per channel. |
| WP-48 | Best-time scheduling and confirmations | `planner/timing/`, `nurture/confirm/` | 08, 14, 17 | Per-channel posting windows learned from the client's own results (prior from public benchmarks, shrunk toward measured data). Booking confirmations and reminders by SMS or WhatsApp only with recorded consent, through the one human-approved send path's pre-approved templates. |

Folded into existing packages: budget follows cost per customer (WP-22 ads desk, recommend only, owner moves money); multi-language variants (WP-06, native review before pilot); short video with voiceover (WP-36, ElevenLabs as an optional provider with a consented voice only); "what stops the loop" list made explicit in WP-20 (cost cap, failed check, missing consent, NEEDS_CHECK send, provider outage).

## Image generation (rule changed by Sai, 23 Nov)

The case against, kept on record: an image is a claim. A generated photo of a dish, a gym floor, a result or a customer shows something that never happened, our fact gate cannot verify pixels, and one discovered fake breaks "Grown, not generated" for every client at once.

How we do it anyway, so that the claim above never applies:
1. Allowed subjects only: backgrounds, textures, abstract or illustrative scenes, mood and seasonal art. Never a person shown as a customer or staff, never the client's product, food, premises or a before/after.
2. Real product, generated scene: when a product must appear, the client's real photo is cut out and composited onto a generated background; the product pixels stay untouched and are checked by hash.
3. Brand contract as input: palette, mood words, photo treatment and do-not list from the confirmed contract go into every prompt; outputs are colour-checked against the contract (CODE check, FAIL off-palette).
4. Many candidates, strict filter: generate 8 to 12, run the vision grader for artefacts (hands, faces, text, warped logos, extra limbs, garbled signage), reject anything with text in the image (type is set by our templates), keep the top 2 for the owner.
5. Provenance: C2PA manifest on every generated asset, provenance recorded in the media leaf (contracts v2 already hashes it), the approver must acknowledge the FLAG, platform AI labels set where the API supports it.
6. Provider behind the registry (current best image model at build time), pinned version, key from the vault, cost caps per client, manual floor is the template render.
"Perfect" means: zero artefact flags, on-contract colours, Sai's design sign-off on the template family, and the blind comparison in QUALITY_BAR section 8 at least level with an agency.

## Order

1. WP-47 morning page and WP-42 quotes and deposits (closes the money loop; pilots feel it first).
2. WP-45 earned auto-replies with WP-39, then WP-44 calls (biggest lead leak for local businesses).
3. WP-43 market signals and WP-48 timing and confirmations.
4. WP-46 generated imagery last, behind its own design review.
These run after the remaining wave 3+ packages; WP-42 and WP-47 can start alongside WP-21.

## Open calls for Sai
- Stripe Connect (Thallo as platform, cleaner, needs a Thallo Stripe account) or each client's own restricted key (simpler, no platform account).
- Telephony: Twilio numbers (we own the setup) or plug into the client's CallRail.
- Which image model to trial, and the cost cap per client per month.
- Whether auto-replies are offered in the pilots or only after them.
