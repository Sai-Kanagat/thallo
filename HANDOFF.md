# Status (13 Nov 2026, stopped at the usage cap: week at 94%)

Product: Thallo, "Grown, not generated." Package is still `cortex_operator`. Contracts 2.3.0 (`docs/contracts.md`). Models: AGENTS.md. Run everything: `bash scripts/ci.sh` (Docker must be running; 1316 tests green on main).

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
