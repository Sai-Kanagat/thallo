# Wave 3+ addendum (lead, 19 Nov 2026): build fast, review at wave end

Read after your package brief and `docs/packages/WAVE2_ADDENDUM.md`; where they disagree, this file wins.

## Mode
Sai's direction: build the whole product now, debug after. So:
- Build the package's full "done when" with working code and tests. Prefer simple, working, tested code over perfect code.
- No per-package review rounds. The lead runs one security and quality audit per wave at the end. You still fix anything the automated commit review flags if it is a real issue.
- Commit working increments often so a usage cut-off loses nothing.

## What exists (read the code, not old briefs)
Contracts 2.6.0 (`docs/contracts.md`). Store with tenant RLS (`thallo/store`). Connections registry and vault (`thallo/connections`), every provider one file with `register(registry)`, five calls, mock, manual floor. Truth library (`thallo/truth`). Gate with content-bound checks, per-media coverage, canonical form v2 (`thallo/gate`). Brand and renderer (`thallo/brand`, `thallo/media`). Copy production (`thallo/copy`). Check stack (`thallo/checks`). Publisher, exactly-once (`thallo/publish`). Web app: FastAPI in `api/`, Next.js in `web/` (identity from Sai's Thallo page; follow `~/.claude/DESIGN.md` and `docs/QUALITY_BAR.md`). Demo: `scripts/demo.sh [--brand sai]`. Plays: `docs/plays/plays_draft.json`.

## Hard rules (unchanged, non-negotiable)
- Nothing publishes, sends a message, spends or creates an account. Every send path stops at the gate and a human approval; messaging and ads are draft or manual unless a human approves each action.
- No network and no keys in tests. Real providers against recorded fixtures plus the contract suite.
- Secrets only from the vault; no setting names a binary, a path, or a host that receives a secret.
- Untrusted text (leads, comments, transcripts, research) only in `untrusted_data`.
- Tenant isolation everywhere; new tables call `app_enable_tenant_rls`.
- Migrations: next free number, never edit applied ones, Supabase-safe (no SUPERUSER, BYPASSRLS). If two packages need a migration, take the number given in your prompt.
- Quality bar: no fabricated numbers; every claim through the truth library; approval rate always shown with outcome per piece.
- Commits: small, plain, NO AI attribution, no em dashes or emoji, never --no-verify, never push.
- Stay inside your owned paths. If you must touch a shared file (contracts, gate, store, api app wiring, web nav), keep it minimal and list it in your report.
- Before reporting: `bash scripts/ci.sh` green (rerun `tests/wp03_truth/test_speed.py` alone if it fails under load) and, if you touched `web/`, `bash scripts/ci_web.sh`.

## Report (under 250 words)
Branch, commits, migration number if any, test counts, what is real versus mocked, shared files touched, what needs Sai.
