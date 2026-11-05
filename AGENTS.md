# Rules for every agent (Claude, Codex, any other)

Read `PLAN.md` first. It is the source of truth for scope and build order.

## Hard limits, in every permission mode
- Never publish, post, send or start outreach. A human does that after explicit approval.
- Never spend money, buy anything, or create accounts.
- Never enter, print or request passwords, API keys or tokens. Secrets live only in `.env`, added by Sai. Never commit them.
- Keep the gitleaks pre-commit hook on. Never bypass it with `--no-verify`.
- Use mock adapters in tests and sandbox data only. No real external calls until a real run is approved.
- Never delete existing work or rewrite shared history.
- Never fabricate data, results, sources or claims. Say what is real, mocked or unverified.
- Keep client and brand data out of this repo.

## How to work
- Stay inside your assigned file scope and branch or worktree. Do not touch contracts without the lead.
- Lowest usage for the highest result: short plans, reuse before writing, deterministic code for rules, sizing and scoring.
- A task is done only when its tests pass and you have reported what you could not verify.
- Check current official docs before building against an outside API.

## Style
- Commit as the configured git user, with no AI co-author or generated-by lines.
- No em dashes or emoji in copy, docs or commit messages.
- Match surrounding code. Small, focused commits.
