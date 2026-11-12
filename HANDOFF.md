# Status (paused by Sai, 12 Nov 2026, 07:50)

Product name: Thallo, tagline "Grown, not generated." The Python package is still `cortex_operator`; rename it before wave 2 fans out. Plan of record: `docs/BUILD_PLAN.md`. Contracts: `docs/contracts.md` (version 2.0.0). Model policy: AGENTS.md (Opus by default; Fable at low effort for contracts, WP-15 play data and bugs that cross three or more modules).

Run everything: `bash scripts/ci.sh` (needs Docker; 448 tests green on main at 3c983c6).

## Wave 1
| Package | State |
|---|---|
| WP-00 contracts | Merged, at 2.0.0 after a Fable review |
| WP-01 data layer | Merged |
| WP-02 connections and vault | Merged |
| WP-03 truth library | Branch `worktree-agent-a84d480e55ac49165`: paused mid-merge of contracts 2.0.0 |
| WP-04 gate | Branch `worktree-agent-ae202a7cd6122358e`: paused mid fix round |

Both paused workers keep their work in `.claude/worktrees/`. Resume each with its remaining list:

**WP-03, still to do:** apply the approved legacy test fix (scratchpad engine_fix.diff: drop digits from two `tests/test_engine.py` texts); add the REVIEW_FLAG tier for ambiguous words (sei, uno, una, prima, secondo, pronoun "one") and universal claims (never, always, every, all, sempre, mai, tutti), hard-blocking only next to a digit, slot, unit or quantity noun; finish the 2.0.0 merge (claim versions: change writes a new row, VOIDED is terminal, lookups take the latest live version); fix two new automated findings: the Italian number-root regex can backtrack catastrophically (make the roots unambiguous or tokenise first, and cap input length), and the multiplier check after a slot runs on raw text, so run it on folded text and add multiplication signs, or reject any non-space, non-punctuation character right after a slot. Then an independent review, and a cross-family review once a Gemini key exists.

**WP-04, still to do:** a keyed MAC on approval grants (`THALLO_GATE_SIGNING_KEY`, block if missing) and correct the AUDIT.md wording; link fail-closed but no false hits on file names ("report.pdf", "Node.js") or on prose with no space after a full stop; `find_unresolved_links()` for WP-07; consent scope (likeness or voice by media kind, given_at before now); `_grant_recorded` checks approved_at, expires_at, canary and chain linkage; one resolver snapshot shared by the hash and the payload; then merge main (2.0.0: bind claim id AND version). Then a final security re-check and merge.

After both merge: run the fake-clock week end to end, then start wave 2 with two Opus workers at a time.

## Waiting on Sai
1. `.env`: `DATABASE_ADMIN_URL` holds only the tail of the Supabase URI. It needs the full `postgresql://postgres.<ref>:<password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres`. Check with `.venv/bin/python scripts/check_env.py` (prints SET or MISSING, never values).
2. Then: apply migrations to Supabase, create the cortex_app login and the cortex_worker login, generate `THALLO_VAULT_KEY`, then the Anthropic and Gemini keys, Postiz, and a private GitHub repo for CI.
3. Platform access applications: WhatsApp Cloud, Google Ads, Business Profile.
4. Pilot consents: done (written, kept out of the repo).

Build log page: https://saikanagat.com/build-log/ (repo ~/code/portfolio-live).

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
