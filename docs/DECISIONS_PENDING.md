# Decisions Requiring the Owner

Each has a recommendation; approving the plan "as recommended" settles all of them.

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| **D-01** | How live Binance behaviour is verified during development. The cloud workspace used for development cannot reach any Binance domain (egress allowlist). | (a) Allowlist Binance domains for this workspace in claude.ai Settings → Capabilities; (b) owner runs provided verification scripts on Windows; (c) develop directly on the Windows machine with Claude Code. | **(a) + (b):** allowlist `api.binance.com`, `data-api.binance.vision`, `fapi.binance.com`, `data.binance.vision` (REST/archives for importer and integration tests); WebSocket soak tests run on Windows via a one-command script whose report is committed as evidence. |
| **D-02** | Futures scope in MVP. | (a) Futures as data only, paper trades spot long-only; (b) also paper positions on USDⓈ-M perps, long and short, 1× leverage, funding modeled. | **(b)** — shorts double the opportunity set and funding/mark data are already ingested; leverage stays 1×. Requires that futures market data is reachable from your location (checked in the M0 spike). |
| **D-03** | Runtime Claude API for signal agents. | Off until later / enable at M5 with key + budget; model choice. | **Enable at M5**, Sonnet 5.5 for both roles, **$1/day hard cap**, mode `confirmed`. Needs an Anthropic API key in local `.env` (separate billing from your Claude plan). |
| **D-04** | Universe and timeframes. | Fixed list vs rule-based; size. | **Rule-based top-10** (DATA_ARCHITECTURE §6), **5m / 15m / 1h** strategy timeframes on canonical 1m bars. |
| **D-05** | Research data splits. | As proposed or adjusted. | Dev 2022–2024, validation 2025, sealed holdout 2026-01-01 → 2026-09-30, forward trial from go-live. |
| **D-06** | A/B protocol coordination with the original system. | Who agrees the shared protocol and when; capital assumptions. | Draft in EVALUATION_PROTOCOL; finalize in M6 with whoever operates the original system; neither side sees the other's signals during the window. |
| **D-07** | GitHub pushes. | Ask every time / standing approval for non-destructive pushes. | Approve pushing this planning commit to `main` now; then **standing approval for non-destructive pushes of tested milestones** (never force-push, never history rewrites). |
| **D-08** | Commit author identity. | Commits authored as "Claude" with a co-author trailer (current) / authored as you. | Keep "Claude" as author — it makes the Claude-only provenance of every change visible for the experiment. |
