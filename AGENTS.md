# Rules for every agent (Claude, Codex, any other)

Read `docs/BUILD_PLAN.md` first. It is the source of truth for scope and build order. `PLAN.md` is superseded.

If you are a worker, your brief is one file in `docs/packages/`. It carries your file scope, your dependencies, your done test and the spec sections you need.

## Build rules (from `docs/BUILD_PLAN.md` section 4.1)
- Contracts first. WP-00 freezes schemas, states, the connection contract, event names and the fake clock. After that, only the lead changes a contract.
- One writer per path. Edit only the paths your package owns.
- Every outside capability is a connection with a manual mode, a mock and a contract test. The core never calls a vendor directly.
- The gate, hashing, claim checks, provenance, disclosure labels and cost ledger sit above every connection. No provider may route around an approval.
- Deterministic code for hashing, scoring, sizing, routing, caps and unlock rules. Models only where language or judgment is needed.
- Approval, claim verification and spend signing are human-only. No agent or API caller may perform them.
- Merge only when tests pass and a separate review finds no blocking bug. Gate, vault, publisher, messaging and API also need a security review.
- After each wave, the fake-clock week must pass end to end.

## Models (Sai, 12 Nov 2026; replaces BUILD_PLAN 4.1 "Models")
- Opus is the default for every work package, worker and review.
- Fable at low effort only for: contract changes and `db/migrations/0001`, drafting the Play A and Play B data in WP-15 (Opus writes the validation code), and bugs that cross three or more modules or a cross-package integration bug stuck for 30+ minutes.
- Never use Fable for HTML generation or simple API wrappers.

## Contracts and CI
- The frozen contracts are in `thallo/contracts/`, described in `docs/contracts.md`. Read that file before writing code.
- Additive schema changes go in your package's own migration. Breaking changes go through the lead (`docs/contracts.md`, "Changing a contract").
- Every provider you write must pass `assert_provider_contract()`.
- Take a `Clock`; never read the system clock. Never import a network or vendor SDK outside `thallo/providers/`.
- Run `scripts/ci.sh` before asking for review. It needs no keys.

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

Quality bar: `docs/QUALITY_BAR.md` applies to every package (brand contract, design craft, imagery, copy, product UI, proof). UI packages need a design review before merge.
