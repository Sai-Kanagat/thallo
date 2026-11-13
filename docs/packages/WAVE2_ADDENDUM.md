# Wave 2 addendum (lead, 13 Nov 2026)

Read this after your own package brief. Where the two disagree, this file wins: the briefs were written before wave 1 existed.

## What exists now
- Contracts 2.2.0: `docs/contracts.md`. `0001_core.sql` is frozen and deployed. A new column is `added("NNNN")` in `models.py` plus a migration; ask the lead for anything on a shared table.
- Store (WP-01): `Database(app_url, clock).tenant(tenant_id)` gives a `TenantRepo` (`insert`, `get`, `list`, `update`, `delete`, `count`, `execute`). Every new tenant table calls `SELECT app_enable_tenant_rls('table');`. Tests that need Postgres copy the per-run role fixture in `tests/wp01_store/conftest.py`. Tenants need `operator_id` and `currency`.
- Connections (WP-02): add a provider as one file `providers/<capability>/<name>.py` with `register(registry)`. Resolve and run through `ConnectionRegistry.run(tenant, request, ...)`; every request needs an `idempotency_key`. Secrets come from `ctx.secret()`, never from settings. Mock providers work only for tenants in `mock_tenants` or with `THALLO_ALLOW_MOCK=1`.
- Truth (WP-03): the writer emits `{{C-0012}}` and never types a number, sign or name. `build(text, tenant_id=, language=, claims=, clock=, ...)` returns the filled text with `slot_spans`, or raises `CompileFailed` with findings. FAIL findings block. FLAG findings become `check_result` rows with verdict FLAG.
- Gate (WP-04): `approve_bundle_from_repo(...)` and `check_publish(...)`; see `gate/CANONICAL_FORM.md` and `gate/AUDIT.md`. Grants and voids return a `GateChange` that the caller writes in ONE transaction. Every edit after a grant must call `void_bundle_approvals`.

## Rules every wave 2 package follows
1. Three verdicts: FAIL blocks, FLAG must be acknowledged by the approver, PASS collapses. Never downgrade a FAIL, and never hide a FLAG.
2. Untrusted text (leads, comments, research, transcripts) goes in `TextRequest.untrusted_data`, never in `instructions`.
3. Money is in the tenant's currency. Only provider costs are in euros.
4. Migrations must run on Supabase: never name the SUPERUSER or BYPASSRLS attributes.
5. Your tests run with no keys and no network. Real providers are tested against recorded fixtures plus the contract suite.
6. Models: Opus builds. Do not use Fable.
7. Run `bash scripts/ci.sh` before you report. Commit small, no AI attribution, no em dashes, do not push.

## Per package

**WP-05 brand system, renderer, design lint.** Tokens are JSON per brand and per script (Latin for en and it, Tamil for ta). Templates live in `brand_templates/`. The renderer is a local IMAGE provider (`providers/image/template_render.py`) behind the registry, so the gate's media hashing sees real bytes: return `MediaResult` with the sha256 of the PNG or PDF you wrote, provenance `TEMPLATE_RENDER`. Design lint emits `check_result` rows in layer CODE. A failing render is FAIL, never FLAG. Headless Chromium must be optional in CI: skip those tests with a clear reason when it is missing, and keep the lint logic testable without a browser.

**WP-06 text providers and copy production.** Providers: Claude, Gemini, an OpenAI-compatible endpoint, a local endpoint, each one file, each passing the contract suite against recorded responses. Model per `TextRole` comes from the connection row, not from code. Copy production: three candidates, each passed through `truth.build`; a candidate that raises `CompileFailed` is repaired by feeding the findings back (at most three loops, counted on `check_result.attempt`), then goes to a human with the failing check named. Store the selection reason on `asset`. Record cost through `cost_ledger` and respect `cost_cap_eur` (short piece USD 0.50, long USD 3, converted at the fixed rate in one constant).

**WP-07 check stack.** Order is fixed: code checks (truth compile, `gate.find_unresolved_links`, slop catalogue, sameness, length), then graders, then the cross-family second reader. Graders and the second reader can only FLAG, never PASS a fact and never FAIL a build. Grader agreement with the reviewer is computed weekly from `edit` and `approval` rows; under 80% over 30 items, the grader's flags are dropped and that is reported. Depends on WP-06 for text calls: start after it merges.

**WP-08 publisher.** The only caller of `check_publish`. Send exactly `decision.payload`: re-hash the bytes you upload and block on any difference. One `publication` per variant (the database enforces it); the idempotency key is `variant_id:approval_id`. Readback compares canonical text and reports image recompression as a warning, not a mismatch. Evergreen bench: use `plan_slot` (state machine `PLAN_SLOT`). Providers: manual pack (exists), Postiz hosted, Postiz self-hosted. Remove the legacy `Gate` publish path and shrink `LEGACY` in `scripts/check_rules.py`. Needs a security review before merge.

**WP-09 web app shell, review deck, week plan, connections screen.** Next.js PWA in `web/`, FastAPI routes in `api/`. Login is by `identity` (email), then a tenant switch across that identity's `app_user` memberships; identity lookups run through owner-side code, never the app role. The API layer is where `SessionVerifier` gets its real implementation: only an interactive session counts as human. The review card shows the root hash it will approve, lists open flags with one tap each to acknowledge, records seconds on screen, and calls `approve_bundle_from_repo`. Any edit calls `void_bundle_approvals` first. Read `~/.claude/DESIGN.md` before any UI work. Needs a security review before merge.

## Order and capacity
Two workers at a time. Start WP-05 and WP-06 together (independent), then WP-08, then WP-07 and WP-09. The package rename `cortex_operator` to `thallo` happens once, by the lead, between waves, when no worktree is open.
