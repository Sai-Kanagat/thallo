# Contracts (WP-00)

Version 1.0.0, frozen 11 Nov 2026. Code lives in `cortex_operator/contracts/`. Every package builds against these files and none edits them.

## What is frozen

| File | Holds |
|---|---|
| `clock.py` | `Clock` protocol, `SystemClock`, `FakeClock` (forward only, timezone-aware UTC), `FAKE_EPOCH` (Monday 5 Jan 2026, 09:00 UTC) |
| `ids.py` | UUIDv7 row ids (`new_id(clock)`), `Id` type, claim code pattern `C-0001` |
| `enums.py` | Every closed vocabulary: 16 capabilities, connection modes, channels, languages (en, it, ta), claim kinds, media provenance, check layers, approval subjects and methods, rejection reasons, edit kinds, touch kinds, consent bases, lead routes, outcome and loss reasons, autonomy levels L0 to L4, human-only actions |
| `actors.py` | `HumanActor`, `SystemActor`, `AgentActor`, `ActorRef`, the `SessionVerifier` protocol, and `require_human(actor, action, verifier=, tenant_id=)` for the nine human-only actions in BUILD_PLAN 2.8 (approve, verify a claim, sign spend, raise a trust level, sign a template, first reply, public reply, new campaign or audience, consent call). A `HumanActor` value proves nothing on its own; only the verifier does |
| `states.py` | Eleven state machines with legal transitions: variant, bundle, claim, idea, lead, spend proposal, rule proposal, job, message, booking, ad. `LEGACY_STATUS` maps the original engine's `Status` |
| `models.py` | One Pydantic model per table, 50 tables. Every entity in BUILD_PLAN 2.3 plus the shared ones (tenant, user, region policy, connection, channel account, message template, job, audit log, event log, cost ledger, autonomy state, prompt version, incident) |
| `events.py` | 36 event names, one typed payload each, and the `Event` envelope. Events carry an `ActorRef`, never a session |
| `connection.py` | The five-call provider contract, request and result types for all 16 capabilities, `check_request`, `check_cap`, `ConnectionChoice` |
| `ddl.py` | Renders `db/migrations/0001_core.sql` from `models.py` |
| `testing/` | `MockProvider` for every capability, sample requests, and `assert_provider_contract()` |

## Rules baked into the types

- A publish request needs an idempotency key, an approval id and the approved content hash.
- A message send needs exactly one authority: a template approval plus the recipient's consent record, or the verified human sending it.
- A request built with `model_construct()` is re-validated at the provider boundary, so validators cannot be skipped.
- An ad spend change carries the signed proposal, its approval and hash, the campaign and the exact signed daily amount. No free-form payload may set status, budget or bid, so ads are created paused and stay paused until a signed change.
- Generated people need a consent record. So does a real person's voice.
- Research results are always marked untrusted. Untrusted text travels as quoted data in `TextRequest.untrusted_data`, never as instructions.
- Every provider refuses another capability's request and refuses to run over `cost_cap_eur`.
- Every foreign key between tenant tables is composite `(tenant_id, x_id)`, so no row can point at another tenant's row, even before row-level security.
- A voided or expired approval, or a hash change at send time, sends the variant back through the check stack, never straight to review.
- One publication per variant, enforced in the database.
- Hooks only receive their own tenant's events.
- No table stores an IP address or a raw secret. Secrets are a `secret_ref` the vault resolves.
- Missing metrics are stored as missing, never as zero.
- Time comes from a `Clock`. `scripts/check_rules.py` fails any product code that reads the system clock or imports a network or vendor SDK outside `cortex_operator/providers/`.

## How to use them as a worker

```python
from cortex_operator.contracts.clock import FakeClock, FAKE_EPOCH
from cortex_operator.contracts.models import Variant
from cortex_operator.contracts.states import VARIANT, VariantState
from cortex_operator.contracts.testing.contract_suite import assert_provider_contract

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

## Migrations

- `0001_core.sql` is generated. Never hand-edit it: change `models.py`, then run `python -m cortex_operator.contracts.ddl > db/migrations/0001_core.sql`. CI fails on drift.
- `scripts/migrate.py` applies files in order, once each, and refuses a file that changed after it was applied.
- Row-level security per tenant arrives in WP-01's migration, not here.

## CI

`scripts/ci.sh` runs lint, the repository rules, the schema drift check, a throwaway Postgres in Docker, and every test. It needs no keys. `SKIP_DB=1` skips the database. The same script runs in `.github/workflows/ci.yml` once the repository has a remote. The existing `.githooks/pre-commit` runs gitleaks, the repository rules and the drift check on every commit.

## Open items, owned elsewhere

- Share proposals (calendar share changes) get their table and approval subject from WP-18 through the lead.
- `scripts/check_rules.py` is a lint, not a security boundary. Aliased clock calls and dynamic imports slip through; review catches those.

- Loss reasons are a starting list. Sai confirms them with each pilot.
- What each consent basis permits comes from region policy rows a lawyer fills.
- The bundle hash canonical form is written by WP-04, not here.
- The legacy engine files listed in `scripts/check_rules.py` `LEGACY` are ported by WP-06, WP-08, WP-17 and the worker package, and each removal shrinks that list.
