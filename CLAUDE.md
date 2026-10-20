# CLAUDE.md — Rules for every Claude session and subagent in this repository

## What this project is
Trading System Claude: a local-first, paper-only crypto research and signal platform built
exclusively with Claude. Read `docs/PLAN_OVERVIEW.md` first, then the doc for the area you touch.

## Non-negotiable rules
1. **Independence.** Never access, copy or reference code, data, parameters, results or signals from
   the original `trading-system` repository. Any consultation of material about it must be logged in
   `docs/INDEPENDENCE_LOG.md`.
2. **Paper only.** No order placement, no signed/private Binance endpoints, no exchange API keys.
3. **No look-ahead.** Decisions at bar close `T` use only data with `close_time ≤ T`; execution
   no earlier than the next bar open. Never touch holdout data outside the holdout guard.
4. **Fail closed.** Unknown, stale or erroring state blocks entries.
5. **Truthful status.** Never say a feature is done, tested or working without linked evidence (test
   output, CI run, recording). Never weaken an acceptance criterion to make a test pass; escalate.
6. **Provenance.** Every value exposed by the API carries a provenance label (ADR-0010). Demo data is
   always labeled `DEMO`.
7. **Agents advise only.** LLM output can approve/downgrade/veto; it never sets prices, sizes, or
   bypasses risk (ADR-0007).
8. **Secrets.** Only in `.env` (git-ignored). Never log, print or send them to the UI or to agents.
9. **Git safety.** No force-push, history rewrite or remote branch deletion. Pushes need owner
   approval as recorded in `docs/DECISIONS_PENDING.md` (D-07).

## Engineering conventions
- Python 3.13 via `uv`; `uv run pytest`, `uv run ruff check`, `uv run mypy src`.
- Frontend: pnpm, TypeScript strict, `pnpm test`, `pnpm build`.
- Time: UTC everywhere; integer epoch milliseconds in storage; ISO-8601 with `Z` in APIs.
- Money in ledgers: `Decimal` quantized to 1e-8; research vectors: float64.
- Unit tests never use the network; integration tests use recorded fixtures; live tests are marked
  `@pytest.mark.live` and run only on the owner's machine or allowlisted environments.
- Commits: Conventional Commits (`feat(sim): …`, `fix(data): …`, `docs: …`), small and tested.
- Module ownership and review flow: `docs/DEV_ORCHESTRATION.md`. Only the Lead edits `src/tsclaude/contracts/`.
- Every task updates `docs/devlog/metrics.csv` and the roadmap status with evidence links.
