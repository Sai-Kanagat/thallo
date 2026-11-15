# Contracts (WP-00)

Version 2.0.0, 12 Nov 2026 (first frozen 11 Nov at 1.0.0). Code lives in `thallo/contracts/`. Every package builds against these files and none edits them.

## What is frozen

| File | Holds |
|---|---|
| `clock.py` | `Clock` protocol, `SystemClock`, `FakeClock` (forward only, timezone-aware UTC), `FAKE_EPOCH` (Monday 5 Jan 2026, 09:00 UTC) |
| `ids.py` | UUIDv7 row ids (`new_id(clock)`), `Id` type, claim code pattern `C-0001` |
| `enums.py` | Every closed vocabulary: 16 capabilities, connection modes, channels, languages (en, it, ta), claim kinds, media provenance, check layers, approval subjects and methods, rejection reasons, edit kinds, touch kinds, consent bases, lead routes, outcome and loss reasons, autonomy levels L0 to L4, human-only actions |
| `actors.py` | `HumanActor`, `SystemActor`, `AgentActor`, `ActorRef`, the `SessionVerifier` protocol, and `require_human(actor, action, verifier=, tenant_id=)` for the nine human-only actions in BUILD_PLAN 2.8 (approve, verify a claim, sign spend, raise a trust level, sign a template, first reply, public reply, new campaign or audience, consent call). A `HumanActor` value proves nothing on its own; only the verifier does |
| `states.py` | Twelve state machines with legal transitions: variant, bundle, claim, idea, lead, spend proposal, rule proposal, job, message, booking, ad (also used by campaign), plan slot. `LEGACY_STATUS` maps the original engine's `Status` |
| `models.py` | One Pydantic model per table, 56 tables. Every entity in BUILD_PLAN 2.3 plus the shared ones: operator and identity above tenants; tenant, user membership, region policy, connection, channel account, message template, campaign, plan slot, job, audit log, event log, event delivery, cost ledger, autonomy state, prompt version, incident |
| `events.py` | 36 event names, one typed payload each, and the `Event` envelope. Events carry an `ActorRef`, never a session |
| `connection.py` | The five-call provider contract, request and result types for all 16 capabilities, `check_request`, `check_cap`, `ConnectionChoice` |
| `ddl.py` | Renders `db/migrations/0001_core.sql` from `models.py` |
| `testing/` | `MockProvider` for every capability, sample requests, and `assert_provider_contract()` |

## Rules baked into the types

- What the truth compile step guarantees, and what it does not. GUARANTEED (structural, complete): no digit in any script prints outside a verified slot; slots cannot combine or be scaled into a number no claim states; a slot fills only from the tenant's latest live claim version. BEST EFFORT (lexical, never complete): number words, superlatives, comparatives and names are caught from per-language lists tuned for precision. The backstops for that layer are the cross-family second reader (WP-07) and the weekly 20-sentence hand audit. No document or pitch may claim more than this.

- Three verdicts, three behaviours. FAIL blocks the build. FLAG (ambiguous number words, universal claims, grader and second-reader doubts) never blocks the build but must be acknowledged one by one by the approver: the gate refuses a grant unless `acknowledged_flag_ids` covers every open FLAG `check_result` on the bundle's current variants, and the acknowledged ids are inside the signed grant. PASS collapses on the review card. A flag is therefore never a silent pass.

- Check results are bound to content (2.5.0). A `check_result` row names the content it checked in `content_sha256`, the variant's leaf hash in the bundle root, computed by the gate's own function (`gate.check_hash`, given the variant as approval will load it plus the repository's link resolver and non-link allowlist, which raises when any input is missing; or `gate.content_hashes` from a repository). The gate counts only rows whose hash is the variant's current one. Among those, per `(variant_id, layer, check_name)`, the rows at the highest attempt decide: any FAIL refuses the grant and blocks publish, every FLAG must be acknowledged. Coverage is per content type, from one table (`gate.COVERAGE`): every variant needs a counting CODE-layer truth row (`truth.compile` or `truth.compile.<kind>`), and a variant with media also a counting CODE-layer design-lint row (a name in `media.lint.CHECKS`); unknown names never cover. A variant missing a required type is not checked: its grant is refused and publish blocks. Check names: `truth.to_check_results` writes, on every run, one row per finding under `truth.compile.<kind>` (FAIL for a BLOCK, FLAG for a FLAG), a PASS under `truth.compile.<kind>` for every kind not raised, and the summary `truth.compile` (FAIL if anything blocked, else PASS), so a rerun at a higher attempt clears a FAIL on unchanged content once a claim was verified. Fixing the content is what clears a FAIL; restoring the old content brings it back. `check_result` is append-only for the app role, so a row cannot be rewritten or deleted, but the app role can still insert, so a forged later PASS for the same content and key is not prevented by the database (see `thallo/gate/AUDIT.md`).

- Money: every client-money column is in the tenant's `currency` (ISO 4217) and carries no currency suffix. Only our own provider costs (`cost_ledger.cost_eur`, `cost_cap_eur`) are in euros.
- Identity: an `identity` is a login (one per email); an `app_user` is that identity's membership and role inside one tenant. A `tenant` belongs to an `operator`. The app role has no access to operator or identity; login and the console reach them through owner-run code.
- Claims are append-only by version. A change writes a new row (version + 1, `supersedes_id`) and the old one goes to VOIDED, which is terminal, so the exact fact a published piece used is never overwritten.
- `job` keeps tenant isolation for the app role. The single worker claims due jobs across tenants through its own role, `cortex_worker`, which can only read and update `job` (policy `worker_queue`, migration 0012). It then does the work as the app role with `app.tenant_id` set from `job.tenant_id`. `job.payload` holds ids only, never client content.
- `event_log` is append-only. Delivery state lives in `event_delivery`, one row per event per consumer.
- A deal may exist without a booking (walk-in or phone sale). A lead can go CONTACTED to WON or LOST, and HELD back to BOOKED for a second meeting.
- Spend is tracked per campaign (`campaign` table); `spend_snapshot.ad_id` is optional for campaign-level rows.
- Messages belong to a person; `lead_id` is optional (newsletter, proof and webinar messages).
- `plan_slot` is shared core: the planner owns its creation, but the publisher's bench, the review deck, measurement and trust all read and write it through the state machine.
- Export and re-import (WP-21): import mints fresh ids. Imported approvals are historical records, never live, and must be re-granted.

- A publish request needs an idempotency key, an approval id and the approved content hash.
- A message send needs exactly one authority: a template approval plus the recipient's consent record, or the verified human sending it.
- A request built with `model_construct()` is re-validated at the provider boundary, so validators cannot be skipped.
- An ad spend change carries the signed proposal, its approval and hash, the campaign and the exact signed daily amount. No free-form payload may carry a status, budget, bid, spend or schedule key at any depth, and every ads provider must force the paused status on create itself, so ads stay paused until a signed change.
- Generated people need a consent record. So does a real person's voice.
- Research results are always marked untrusted. Untrusted text travels as quoted data in `TextRequest.untrusted_data`, never as instructions.
- Every provider refuses another capability's request and refuses to run over `cost_cap_eur`.
- Every foreign key between tenant tables is composite `(tenant_id, x_id)`, so no row can point at another tenant's row, even before row-level security.
- A voided or expired approval, or a hash change at send time, sends the variant back through the check stack, never straight to review.
- One publication per variant, enforced in the database.
- Hooks only receive their own tenant's events.
- No table stores an IP address or a raw secret. Secrets are a `secret_ref` the vault resolves.
- Missing metrics are stored as missing, never as zero.
- Time comes from a `Clock`. `scripts/check_rules.py` fails any product code that reads the system clock or imports a network or vendor SDK outside `thallo/providers/`.

## How to use them as a worker

```python
from thallo.contracts.clock import FakeClock, FAKE_EPOCH
from thallo.contracts.models import Variant
from thallo.contracts.states import VARIANT, VariantState
from thallo.contracts.testing.contract_suite import assert_provider_contract

VARIANT.check(VariantState.REVIEW, VariantState.APPROVED)  # raises IllegalTransition if not legal
assert_provider_contract(MyProvider(...), FakeClock(FAKE_EPOCH))  # every provider you write
```

## Changing a contract

**Additive, the owning package may do it in its own migration without asking:**
- a new nullable column, or one with a default, on a table the package's module writes
- a new table that only that package uses (numbered `NNNN_wpXX_name.sql`, after `0001`)
- a new index

**Breaking, only the lead does it, after stopping the affected workers:**
- renaming or removing a table, column, enum value, state, transition, event or request field
- changing a column type or nullability
- any change to `connection.py`, `actors.py` or `clock.py`
- any new enum value, state, transition or event name (so every package sees the same list)

The lead bumps `CONTRACT_VERSION` in `contracts/__init__.py` on every breaking change and lists it in the table below.

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-11-11 | First freeze, after the independent review's 5 blocking findings were fixed |
| 1.0.1 | 2026-11-11 | AdsRequest payload check made recursive (security review). Tightening only: no caller change |
| 2.5.0 | 2026-11-14 | Check results are bound to content (lead ruling): `check_result.content_sha256` NOT NULL, the variant's leaf hash in the bundle root (migration 0021; rows written before it carry the marker `unbound-before-0021`, which never matches, so they stop counting). `check_result` is append-only for the app role (revoke plus a trigger). `attempt` is at least 1. Every writer passes the hash: `truth.to_check_results`, `media.lint.to_check_results` and copy production. Post-freeze bounds on 0001 columns use `tightened("000N")` |
| 2.4.0 | 2026-11-13 | `HumanOnlyAction.CHANGE_CLAIM`; claim EXPIRED to VOIDED when superseded. No schema change |
| 2.3.0 | 2026-11-13 | `consent.extra_scopes` (migration 0003): one consent can cover likeness and voice. Use `Consent.scopes` |
| 2.2.0 | 2026-11-13 | Review flags need acknowledgement: `approval.acknowledged_flag_ids` (migration 0002, first post-freeze change). Post-freeze columns use `added("000N")` in models.py |
| 2.1.0 | 2026-11-12 | From WP-03 and WP-04 reviews: `ConsentScope` closed enum on `consent.scope`, `Media.has_voice_track`, `Claim.expiry_reason` |
| 2.0.0 | 2026-11-12 | Breaking, from a Fable review before the first real database: tenant currency and no `_eur` on client money; operator and identity above tenants; job as a cross-tenant queue (0012); event_delivery replaces event_log.delivered_at; deal without booking plus lead CONTACTED to WON/LOST and HELD to BOOKED; campaign table; message.person_id; claim versioning with terminal VOIDED; plan_slot. Affects WP-01 (merged, updated), WP-02, WP-03 and WP-04 (merge main) |
| 1.1.0 | 2026-11-11 | Additive, from WP-02 and WP-03 requests: `RunResult.status` and `task` for manual runs, `MediaProvenance.PERSON_RECORDING` and `PERSON_FILE`, `ClaimKind.OFFER`, `claim.rejected` event, `ClaimChangedPayload.voided_variant_ids`, optional foreign keys default to None |

## Migrations

- `0001_core.sql` is generated and is now FROZEN: it was applied to the real Supabase database on 12 Nov 2026 at contracts 2.1.0. Every schema change from here is a new numbered migration (`0002` to `0009` are reserved for lead-owned core changes), and `models.py` changes with it in the same commit. Migrations must run without a true superuser: never name the SUPERUSER or BYPASSRLS attributes; check them instead. Never hand-edit it: change `models.py`, then run `python -m thallo.contracts.ddl > db/migrations/0001_core.sql`. CI fails on drift.
- `scripts/migrate.py` applies files in order, once each, and refuses a file that changed after it was applied.
- Row-level security per tenant arrives in WP-01's migration, not here.

## CI

`scripts/ci.sh` runs lint, the repository rules, the schema drift check, a throwaway Postgres in Docker, and every test. It needs no keys. `SKIP_DB=1` skips the database. The same script runs in `.github/workflows/ci.yml` once the repository has a remote. The existing `.githooks/pre-commit` runs gitleaks, the repository rules and the drift check on every commit.

## Open items, owned elsewhere

- Lead decision (11 Nov, from the WP-01 review): `tracked_link.slug` and `whatsapp_code` stay globally unique, because a redirect or a coded chat must find its tenant before any tenant is set. WP-12 allocates and resolves them only through narrow SECURITY DEFINER functions with a pinned search_path (random codes, never sequential), so a tenant cannot probe or squat another tenant's codes. No package may use a BYPASSRLS role for this.

- Share proposals (calendar share changes) get their table and approval subject from WP-18 through the lead.
- `scripts/check_rules.py` is a lint, not a security boundary. Aliased clock calls and dynamic imports slip through; review catches those.

- Loss reasons are a starting list. Sai confirms them with each pilot.
- What each consent basis permits comes from region policy rows a lawyer fills.
- The bundle hash canonical form is written by WP-04, not here.
- The legacy engine files listed in `scripts/check_rules.py` `LEGACY` are ported by WP-06, WP-08, WP-17 and the worker package, and each removal shrinks that list.
