# Status

The plan of record is `docs/BUILD_PLAN.md` (11 Nov 2026). It replaces `PLAN.md`.

The build is split into 42 work packages in 8 waves. Each package has its own brief in `docs/packages/`. Nothing from the new plan is built yet. The existing engine in `cortex_operator/` runs on mocks and its tests are the first regression suite.

Run tests: `.venv/bin/python -m pytest -q`

MonoCode failed on its last assignment card. If it is used again, try it on WP-01 before giving it a whole wave.

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
