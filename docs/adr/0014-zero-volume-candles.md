# ADR-0014: Zero-volume candles are classified, never imputed or removed

- **Status:**
  - The classification (rules 1–3, 5, 6) is **Accepted** (owner decision D-11/P-3, 2026-10-28).
  - Rule 4 (no fills on zero-trade candles) is **approved for implementation, subject to validation** (D-12/P-4,
    2026-10-28, after the O-4 live evidence passed). Rules 4 and 5 are implemented in E-1 (`task/m2-e1`,
    simulator `sim-v1`, `4e88ba0`), integrated locally into `feat/mvp-4-dashboard` (merge `03c9158`, 2026-10-30; not pushed, not in `main`); see the
    [E-1 devlog](../devlog/2026-10-28-m2-e1-no-fill-and-interruptions.md). On that branch the simulator fills
    nothing on zero-trade candles. Implemented policy, still subject to validation; not formal M2 acceptance.
  - The QA classification (rule 2) is implemented in E-2, authorized by the owner on 2026-10-30,
    committed and integrated locally
    (`e973f86`; not pushed, not in `main`), with its targeted regression and independent review passed
    (final verdict APPROVE WITH NOTES; [E-2 devlog](../devlog/2026-10-30-m2-e2-zero-trade-qa.md)).
    Some of its mutants ran on a prior code revision. O-7 has not run, M2 is not formally accepted, and no
    statistical strategy validation is implied.
    Existing QA reports keep their meaning (QA report version 2 is explicit); the O-7 re-runs of B03,
    B10 and B12 are not authorized.
- **Relates to:** ADR-0004 (canonical 1m bars), ADR-0005 (one simulator), ADR-0013 (exchange interruptions),
  ADR-0015 (QA open exception).

## Context
Binance publishes a 1m candle for a minute without trades: `trades = 0`, volume 0, and O=H=L=C equal to the
previous close. Observed in our data:
- **HBAR 2024–25 (B12):** 445 zero-trade minutes in 134 runs of at least 3 minutes. The sampled runs are 3–5
  minutes long, which is thin trading.
- **Leading zero-trade minutes** in a 15m bucket explain all five AVAX/HBAR native-versus-derived open
  differences (ADR-0015).
- **2023-03-24:** 71 zero-trade minutes in each of the 5 canonical spot symbols checked, immediately before the
  exchange halt (ADR-0013).
- **Simulator behaviour.** The simulator treats such a candle like any other. Its slippage model is fixed (it
  ignores volume), so an order whose trigger or scheduled execution falls on a zero-trade candle **fills at a
  price at which nobody traded**.

There are three different situations behind "no volume", and they must not be merged:

| Class | Definition (all measurable from the bar and its neighbours) | Meaning |
|---|---|---|
| **Z1, genuine no-trade minute** | a published candle with `trades = 0`, volume = quote volume = taker volumes = 0, and O=H=L=C = the previous candle's close; not inside a registered interruption | The market was open, but nobody traded |
| **Z2, exchange-interruption filler** | a Z1-shaped candle inside a registered exchange interruption (ADR-0013) | The market was not available |
| **M, missing observation** | no candle published, or a candle rejected as malformed | No information. It is a gap: registered, never interpolated (unchanged) |
| **A, anomalous zero-volume** | volume 0 but `trades > 0`; or `trades = 0` with non-flat OHLC; or a flat price different from the previous close | A source or parse problem. A QA finding to investigate |

## Decision (proposed)
1. **Storage is unchanged.** Every published candle is kept exactly as published. Zero-trade candles are never
   removed, and missing minutes are never filled.
2. **QA classifies; it does not just count:**
   - Each zero-volume minute is classified Z1, Z2 or A.
   - Z1 runs keep the existing run statistics (count and minutes, for runs of at least 3).
   - A long Z1 run (≥ 30 minutes, configurable) is flagged. If the same run appears in every symbol of the
     market, it becomes a **candidate** for the ADR-0013 registry; it is never waived automatically.
   - Class A rows are listed individually and block acceptance of the dataset until explained.
   - A zero-trade candle that follows a missing minute cannot be checked against the previous close, so it is
     class A (conservative).
   - The classifier uses the full Z1 definition (`trades = 0` and every volume field 0 and flat at the previous
     close), not just `volume == 0` as the current QA run counter does.
3. **Features and aggregation are unchanged.**
   - Z1 candles are real observations of "no trade" and are aggregated as now. Live is **expected** to receive
     the same candles; this is still to verify (below).
   - The derived 15m open is the open of the first 1m candle, even when it is a Z1 filler. That differs from
     Binance's native 15m open by design (see ADR-0015).
4. **Simulation (needs its own approval, because it changes results):**
   - No fill on a Z1 or Z2 candle. An entry or exit that would trigger or execute on a zero-trade candle waits
     for the next candle with `trades > 0`, and is evaluated there with the existing gap-fill rules (the worse of
     open and level).
   - Market entries scheduled at the open of a zero-trade candle execute at the next traded open.
   - `valid_until` is never extended; an entry that expires while waiting is cancelled.
   - The rule applies in backtests and paper alike, because the simulator is shared.
   - It needs a simulator version bump and golden tests, and it changes existing results (no local runs exist
     today).
5. **Reporting.** Backtest reports show the share of zero-trade minutes in the evaluated range per symbol, and
   the number of trades whose entry or exit waited because of rule 4.
6. **No silent imputation, anywhere:**
   - No step may replace a zero-trade candle with a modelled price.
   - No step may treat a missing minute as zero-trade.
   - No step may turn a Z1 run into a gap.

## Still to verify
- **Live candles: verified (O-4 PASS, 2026-10-29 01:07–02:07 UTC, owner run).** The live spot kline stream
  delivers a closed zero-trade candle (Z1: `n = 0`, all volumes 0, flat at the previous close) for each minute
  without trades, identical to REST: 70 of 70 REST zero-trade minutes on ZRX/CVC/OGN were delivered, with no
  field mismatch and no missing minute. No live-builder change is needed. Evidence:
  [O-4 devlog](../devlog/2026-10-28-o4-owner-capture.md).
- **USD-M archives:** whether they follow the same convention. Still open.

## Consequences
- **Gains:** the three meanings of "no volume" become visible and auditable, and backtests stop crediting fills
  that no counterparty could have provided.
- **Implementation it needs** (not authorized): QA classification (marketdata), the simulator no-fill rule (sim,
  with a version bump), report fields (research), and tests for every class.

## Alternatives rejected
- **Dropping zero-volume candles:** creates artificial gaps and breaks live/backtest parity.
- **Forward-filling or interpolating:** fabricated data.
- **Treating every zero-volume run as a gap:** turns a quiet but open market into missing data, and would grade
  illiquid symbols X.
