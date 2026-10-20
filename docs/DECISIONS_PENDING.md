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
