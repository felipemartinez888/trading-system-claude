# Decision Record (owner decisions)

**2026-10-19 18:40 (UTC-6) — the owner approved all recommendations**, with an added priority:
*signal effectiveness and the highest win rate backed by reliable backtesting, pure mathematics.*
That priority is formalized in [ADR-0012](adr/0012-win-rate-margin-objective.md) and
[RESEARCH_PROTOCOL §5](RESEARCH_PROTOCOL.md#5-selection-objective-owner-priority).

| ID | Decision | Status | Resolution |
|---|---|---|---|
| **D-01** | How live Binance behaviour is verified | ✅ Approved · ⏳ **owner action pending** | Allowlist `api.binance.com`, `data-api.binance.vision`, `fapi.binance.com`, `data.binance.vision` for this workspace in claude.ai Settings → Capabilities; WebSocket soak tests run on the owner's Windows PC via a one-command script whose report is committed as evidence. |
| **D-02** | Futures scope in MVP | ✅ Approved | Paper positions on USDⓈ-M perps, long and short, 1× leverage, funding modeled; spot long. Subject to futures data being reachable from the owner's location (M0 spike). |
| **D-03** | Runtime Claude API | ✅ Approved | Enabled at M5, Sonnet 5.5 for both roles, $1/day hard cap, mode `confirmed`. Signals themselves are purely mathematical; agents only veto/downgrade. Requires an Anthropic API key in local `.env`. |
| **D-04** | Universe and timeframes | ✅ Approved | Rule-based top-10; 5m / 15m / 1h strategy timeframes on canonical 1m bars. |
| **D-05** | Research data splits | ✅ Approved | Development 2022-01-01 → 2024-12-31; validation 2025; sealed holdout 2026-01-01 → 2026-09-30; forward trial from go-live. |
| **D-06** | A/B protocol coordination | ✅ Approved | Draft in EVALUATION_PROTOCOL; finalized in M6 with whoever operates the original system. |
| **D-07** | GitHub pushes | ✅ Approved | Planning commit pushed; standing approval for non-destructive pushes of tested milestones. Never force-push or rewrite history. |
| **D-08** | Commit author identity | ✅ Approved | Commits authored as "Claude" with co-author trailer. |

New decisions that require the owner will be appended below.

## Lead decisions (technical, within approved scope)

| ID | Date | Decision | Context |
|---|---|---|---|
| L-01 | 2026-10-20 | `Provenance.SYSTEM` added for the app's own runtime state (session, chain, coverage, stream health); `LIVE` is reserved for market data observed from the exchange. | M0-T4 review F6; M1-T1 applies it to stream health. |
| L-02 | 2026-10-20 | A minute counts as *observed* only if it lies entirely inside a monitoring interval (`start ≤ open_time` and `close_time < end`). Partial overlap is not observation. | M1-T1 review N1 — conservative: observation coverage is never invented. |
| L-03 | 2026-10-20 | ADR-0012 Amendment 1: certification sample floors (100 design p*, 300 realized p*), realized-payoff uncertainty carried via joint bootstrap, degenerate samples fail closed, alpha ≤ 0.10, power ≥ 0.80, one test per admission gate. | M2-T3 statistical review F1–F3. |
| L-04 | 2026-10-20 | Same-bar entries may be stopped out in their fill bar but never book a take-profit in it; TP eligible from the next 1m bar. Trades spanning data gaps are flagged and capped. | M2-T2 causality audit F1: the O→nearer-extreme path booked unflagged same-bar wins (4.7 % of winners in random scenarios), biasing win rate upward. |
| L-05 | 2026-10-20 | ADR-0012 Amendment 2: dependence always accounted for (estimated block length, min of CP and block-bootstrap bounds), design p* checked against realized payoffs, trial counts from the registry. | M2-T3 re-review N1–N4. |
