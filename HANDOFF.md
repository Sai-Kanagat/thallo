# Status (13 Nov 2026, stopped at the usage cap: week at 94%)

Product: Thallo, "Grown, not generated." Package is `thallo` (renamed from `cortex_operator`). Contracts 2.4.0 (`docs/contracts.md`). Models: AGENTS.md. Run everything: `bash scripts/ci.sh` (Docker must be running; 1,958 tests green on main, 14 Nov).

## Wave 1: complete and merged
WP-00 contracts, WP-01 data layer, WP-02 connections and vault, WP-03 truth library, WP-04 gate. The Supabase database is live with migrations 0001, 0002, 0003, 0010, 0011, 0012, 0020 and two restricted logins (`scripts/provision_db.py`). `0001_core.sql` is frozen: schema changes are new numbered migrations with `added("NNNN")` in models.py.

## Done since (13 Nov, evening)
Wave 1 follow-up merged: Postgres adapters for gate, audit and claims, `write_gate_change`, the end-to-end flow test (`tests/integration/`), single-statement gate reads, a streamed audit chain. Truth library redesigned after a second review: a complete structural layer and a precision-tuned lexical layer (83 ordinary sentences: 0 blocks, 0 flags; 117 bypass strings: 0 misses). Contracts 2.4.0. Main: 1679 tests green. Migration 0013 is applied on Supabase.

## Next, in order (lead orchestrates only; Opus builds, Sonnet or Haiku for mechanical work; two workers at most; stop at 70% of the 5-hour window)
1. Rename the package to `thallo` (Sonnet; remove the old worktrees under `.claude/worktrees/` first; update pyproject, imports, scripts, docs, CI, `LEGACY` paths).
2. Wave 2 per `docs/packages/WAVE2_ADDENDUM.md`: WP-05 and WP-06, then WP-08, then WP-07 and WP-09. Truth API changed: `compile_text` and `build` take `tenant_id`, `allowed_terms`, `tenant_currency`; read `truth/README.md`.
3. End-of-wave audit: full CI, security review of gate, vault and publisher, Supabase against the code, GitHub CI, and the build-log page (new tagline, real status).
4. WP-15 uses `docs/plays/plays_draft.json`.

## Open items
- Truth, known gaps (lexical layer, backstopped by the WP-07 second reader): two-plus-two letter splits ("be st"), reversed text, single-letter Roman numerals, an ambiguous word followed by three or more adjectives, idioms ("golden jubilee"). Known false block: "Sei nuovo cliente?". Country flag emoji do not block (accepted). All Tamil lists need a native reader.
- Next contract batch: `Claim.bound_noun`; a per-tenant `allowed_terms` table (human-approved); a key id on grants and Ed25519 for the publisher; audit anchor recording (WP-20); a database trigger making claim versions append-only.
- `gate/tlds.txt` is partial, so the prose exemption is off, pending Sai's yes to download https://data.iana.org/TLD/tlds-alpha-by-domain.txt.
- Main is ahead of GitHub (`Sai-Kanagat/thallo`); Sai pushes or says "push".

## Waiting on Sai
1. Reset the Supabase database password (part of it appeared in a transcript), update `DATABASE_ADMIN_URL`, rerun `scripts/provision_db.py`.
2. Back up `THALLO_VAULT_KEY` and `THALLO_GATE_SIGNING_KEY` from `.env` in a password manager.
3. `ANTHROPIC_API_KEY` and `POSTIZ_API_KEY` in `.env`; the rest of `docs/SETUP_FOR_SAI.md` (access applications first).
4. Answers to the four questions at the end of `docs/plays/plays_draft.json`, and the decisions table in the setup guide.
5. Yes or no on the IANA list download.

Usage: stop at 70% of the 5-hour window unless Sai says otherwise. The Fable reset expires 17 Nov; the full reset expires 10 Dec. Build log: https://saikanagat.com/build-log/ (still shows the old tagline and wave 0).

# Kickoff prompt for the orchestrator

```
You are the lead engineer for Cortex Operator. Read docs/BUILD_PLAN.md in full, then AGENTS.md.

Your job is to coordinate, not to write most of the code.

1. Do WP-00 yourself. Freeze the contracts and get CI green with no keys.
2. Then run the waves in order. Inside a wave, assign each work package to one worker
   in its own git worktree and branch. Give the worker its file from docs/packages/,
   the contracts, and AGENTS.md.
3. A worker may edit only the paths its package owns. If it needs a contract change,
   it stops and asks you.
4. Merge a package only when its tests pass, a separate review agent finds no blocking
   bug, and (for gate, vault, publisher, messaging, API) a security review passes.
5. After each wave, run the fake-clock week end to end and fix what breaks before
   starting the next wave.
6. Never publish, send, spend, create accounts or handle secrets. Use mock connections.
   Collect everything that needs Sai into one list per wave.
7. Report outcomes only: what is real, what is mocked, what is waiting on Sai,
   and what you could not verify.

Start with WP-00 and show me the contract list before you write it.
```

# Waiting on Sai

See `docs/BUILD_PLAN.md` section 4.6 for the list per wave. Before wave 1: a Supabase project with keys in `.env`, written consent from each pilot business, and access applications for WhatsApp Cloud, Google Ads and Business Profile.

## Security fixes merged 14 Nov (renderer settings, key origin, over-cap error)
Merged after an independent security review, nothing blocking, CI 2,044 passed. The Chromium binary and all folders are out of tenant settings; a text provider's key goes only to a constant origin (Claude, Gemini) or an operator-listed one; `CostOverCapAfterRun` is no longer a `CostCapExceeded`.
New operator env vars (Sai adds them to `.env`): `THALLO_BRAND_ROOT` (one folder per tenant id holding `brand/`, `templates/`, `photos/`, `renders/`; unset means the renderer is unavailable for everyone) and `THALLO_OPENAI_COMPATIBLE_ORIGINS` (comma-separated origins, no path).
Rule for every provider: connection settings are tenant-writable, so no setting may name a binary, a filesystem path or a host that decides where a secret goes.

Still open from that review, must land before tenants can upload template files (WP-09 or any upload path):
1. SHOULD FIX. Templates now live in the tenant's own folder, and the render page still allows inline script and `file:` images and fonts (`thallo/media/render.py`, `thallo/brand/templates.py`). A tenant-written template can render another tenant's images or any local image or font into its own output. Fix: only operator-owned templates until the page policy confines `file:` to the tenant folder (or assets are inlined as data URLs and `file:` is dropped).
2. NOTE. `base.css`, `manifest.json`, `channels.json`, `brand.json`, `script/*.json` are opened without the inside-the-folder check; folders are checked once then used later. Both matter only if an upload path can create symlinks: the upload path must refuse symlinks.
3. NOTE. The tenant-id pattern in `template_render.py` uses `$`, which accepts a trailing newline; use `\Z` or `fullmatch`.
4. NOTE. `_grade` in `thallo/copy/produce.py` catches `Exception`; a grader that calls the registry needs a cost contract first (WP-07).

## Lead ruling: check results are bound to content (contracts 2.5.0, to build)
Why: the gate fix on branch `worktree-agent-af29c4c07c8cfae8c` (commit ded0f2a) refuses a grant on a latest FAIL, but review showed a FAIL can never be superseded (the compiler writes FAIL under `truth.compile.<kind>` and a clean rerun writes PASS under `truth.compile`), and nothing ties a check row to the content it checked. Do not merge ded0f2a alone.

Design:
1. `check_result.content_sha256` (new column, NOT NULL, `added("0021")` plus migration `0021_check_content_hash.sql`): the per-variant canonical hash the gate uses in the approval root, computed by the same gate function. Every writer sets it (truth `to_check_results`, copy production, design lint).
2. The gate reads only rows whose `content_sha256` equals the variant's current hash. Rows for older content are ignored, so fixing the content is what clears a FAIL. Among matching rows, per `(variant_id, layer, check_name)` take the highest attempt: any FAIL there refuses the grant; every FLAG there must be acknowledged.
3. Coverage: a variant with no matching CODE-layer row is "not checked" and the grant is refused. The truth compiler always writes at least a PASS row.
4. `check_result` becomes append-only for the app role (no UPDATE, no DELETE), in the same migration, following the pattern in `0010_wp01_rls.sql`. `attempt` is `ge=1` in the model and a CHECK in the migration.
5. `check_publish` re-reads check results for the payload's content hash and blocks on any FAIL, on missing coverage, and if the read raises.
Consequence: rows written while drafting stop counting once media or text changes; the WP-07 check stack reruns the code checks on the finished variant.

## Gate content binding: review result and lead rulings (14 Nov, late)
Built on branch `worktree-agent-af29c4c07c8cfae8c` (commits ccfd7ca, 6812e14, 75e33f9; CI there 2,003 passed, 5 skipped; migration 0021 run on Docker only). NOT merged: the security review found one blocking gap. Review repro: scratchpad `gate/repro.py` of the lead session.

Rulings for the fix round (same branch, merge current main first):
1. BLOCKING. Coverage is defined by the gate, per content type, not by "any CODE row". A variant with text needs at least one counting row named `truth.compile` or `truth.compile.*` for its current hash. A variant with media also needs at least one counting design-lint row. The required families live in one table in `thallo/gate/checks.py`. An unknown check name never satisfies coverage. Tests: a lint PASS alone on a text variant is refused; an invented name is refused; copy rows written before media was attached do not cover the variant after.
2. A rerun must be able to clear a FAIL on unchanged content when the world changed (a claim got verified; claim state is not in the hash). So no "FAIL is sticky forever". Instead `to_check_results` always writes one row per kind the compiler can raise: FAIL or FLAG when raised, PASS when not, under the same `truth.compile.<kind>` name, plus the summary `truth.compile` row. Highest attempt per key then works for every caller, not only copy production. Test: unverified claim FAIL, verify the claim, rerun at attempt 2, grant.
3. Copy production must not hash with guessed inputs. Remove the `_no_links` fallback: the content hash for a check row comes from one gate function given the variant as approval will see it (text, media, tracked links, resolver, allowlist). If the caller cannot supply those, raise; never write a row that can never match.
4. Add a unit test that hands `review_checks` rows with mixed hashes directly, so the gate's own filter is exercised without the memory repository filtering first.
5. Known limit, accepted for this merge and queued as its own package before WP-09: the app role can insert a PASS under a real check name for content never honestly checked. Real fix: a `cortex_checker` role that alone may INSERT into `check_result`, INSERT revoked from `cortex_app`, checks run through owner-side or worker-side code. Needs a design note first (which process runs checks, how it gets the tenant).
Then: a second independent security review, CI green, merge. Tell Sai before 0021 goes to Supabase. A future tenant-erasure job must drop the append-only trigger inside its own migration.

## Vault and secrets audit (15 Nov, independent, read-only)
Sound: AES-256-GCM with a fresh nonce per seal; ciphertext bound to tenant, ref and version; fails closed on a missing or wrong key; rotation works; `vault_secret` has forced RLS; the `Secret` wrapper redacts everywhere; existing tests cover cross-tenant decrypt, wrong key, tampering and no plaintext in logs.
1. FIXED and merged 15 Nov (reviewed, nothing blocking): a secret with a newline or other illegal header character made `http.client` raise a `ValueError` that quotes the header, key included (`thallo/providers/text/base.py`). Fix: validate before building headers, catch and re-raise without the message, and `Vault.put` refuses such values.
2. SHOULD FIX. `scripts/provision_db.py` sends role passwords as plaintext `PASSWORD` literals, which can land in the Postgres log. Send a client-computed SCRAM verifier. Do this before Sai reruns provisioning.
3. SHOULD FIX. The app role can DELETE then INSERT its own `vault_secret` row at an old version, bringing back a revoked key (own tenant only). Revoke DELETE from the app role or keep a high-water mark.
4. SHOULD FIX. `job.payload` "ids only" is a documented rule with no enforcement, and `cortex_worker` reads every tenant's jobs. Enforce in the model (allow only id-shaped values).
5. NOTE. Settings secret scanner: misses split keys, userinfo URLs without a colon, secrets under 32 characters; rejects legitimate long paths and hex ids. Hygiene only, secrets belong in the vault.
6. `.env.example` was missing 13 variables; added and merged.
7. Follow-up: `writer.py`, `postiz.py`, `notion.py` (older clients outside `thallo/providers/`) put env keys into urllib headers with the same newline risk; route them through `check_header_secret`. NOTE: the transport refuses non-ASCII keys the vault accepts; align `Vault.put` with it.
Not audited: image, Postiz and Notion transports; a live Postgres run.
