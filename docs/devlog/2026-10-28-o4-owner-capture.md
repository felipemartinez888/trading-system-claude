# Owner validation O-4: live zero-trade kline capture (read-only)

- **Purpose** (owner decision D-12 / ADR-0014 rule 4): before the no-fill rule gets final execution semantics,
  check how Binance's **live** spot kline stream reports a minute without trades. Does it deliver a closed candle
  with `n = 0`, flat at the previous close, as the archive does? Is anything missing compared with REST?
- **Run by the owner, not by Claude.** **Normal PowerShell; no administrator rights.** If Windows asks for
  elevation, cancel.
- **Tool:** `scripts/owner_checks/o4_zero_trade_capture.py` (E-4(a); uncommitted; 15 offline tests).

## What the tool can and cannot do
- **Hosts:** it contacts only Binance's **market-data-only** hosts: `data-stream.binance.vision` (WebSocket) and
  `data-api.binance.vision` (REST). They serve public data and cannot accept orders.
- **No credentials or account access:** no API key, no signed request, no account or order endpoint. The tests
  check this in the source.
- **No connection to the app:** it does not import the app (`tsclaude`), does not call the local API, and does
  not touch the paper runtime, the event store, the lake, `data/`, `data_batches/` or the 2026 holdout. Any time
  window overlapping 2026-01-01 → 2026-10-01 is refused.
- **Writes:** only into a **new** output folder, and it refuses to overwrite one that exists.
- **Bounded:** at most 120 minutes and at most 5 symbols.
- **Running beside the app:** it is safe while the app runs; it only adds one public WebSocket connection. For
  the cleanest timing evidence you may stop monitoring first, but this is not required.

## Step 1: preflight (2–3 minutes; chooses the symbols)
A minute without trades is rare on liquid pairs (BTC had none in 2022–2025 outside the 2023-03-24 outage; HBAR
about 0.4 per hour). The capture is only useful on symbols that have **several zero-trade minutes per hour**.
The preflight measures that from the last 24 hours of public REST data.

```powershell
# NORMAL — no administrator. Read-only REST queries; writes nothing.
cd C:\Users\felip\Projects\trading-system-claude
.\.venv\Scripts\python.exe -I scripts\owner_checks\o4_zero_trade_capture.py preflight --symbols HBARUSDT,<SYMBOL2>,<SYMBOL3>,<SYMBOL4>,<SYMBOL5>
```
- **Which symbols to try:** any spot USDT pairs you suspect are thin, up to 5 per run. You can repeat the
  preflight with other candidates. An unknown or delisted symbol is reported as `unavailable` and skipped.
- **The clock line:** if it shows `server - local` beyond ±1000 ms, note it. The capture records it anyway.
- **Choosing:** pick 1–5 symbols with the highest "per hour" values, ideally ≥ 3 per hour each.

## Step 2: capture (60 minutes, unattended)
```powershell
# NORMAL — no administrator. Read-only live capture; writes one new folder under evidence\owner_checks\.
cd C:\Users\felip\Projects\trading-system-claude
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
.\.venv\Scripts\python.exe -I scripts\owner_checks\o4_zero_trade_capture.py capture --symbols <CHOSEN,SYMBOLS> --minutes 60 --out-dir "evidence\owner_checks\o4_$stamp"
```
- **To abort:** press **Ctrl+C** once. The tool stops, reconciles what it has with REST, and writes the report
  with `"aborted": true`. The connection events up to that moment are kept.
- **Closing the window** also stops it, but then only `messages.jsonl` exists, with no `summary.json`.
- **Avoid sleep:** keep the PC from sleeping during the hour. A sleep shows up as a disconnect or stall in the
  report.
- **Network failures:** if the network fails at the end, `summary.json` is still written. The error is listed
  under `errors` and the REST reconciliation is omitted.
- **Quiet markets:** a market without trades is not treated as a dead connection. The tool reconnects only
  after 120 s without any message; the WebSocket keepalive detects real disconnections.
- **Output folder:** `--out-dir` must be a new folder inside `evidence\owner_checks\`; anything else is
  refused.
- **Optional:** note the WHEA count before and after, as in O-2/O-3.

**Resources:** one WebSocket connection, a few REST requests (negligible weight), negligible CPU, and a few MB
on disk. Memory is small because only closed candles are kept; it was not measured.

## Output (`evidence\owner_checks\o4_<stamp>\`)
| File | Content |
|---|---|
| `summary.json` | The report: provenance (git HEAD, dirty count, script SHA-256, Python and library versions, platform), symbols, WebSocket URL, start and end, clock offset at start and end, connection events (connects, disconnects with reasons, stalls, back-offs), and per symbol: closed candles versus judged minutes, missing closed minutes, duplicates and conflicts, maximum message gap, receipt latency (p50/p95/max), **zero-trade closed candles with their class (Z1 or A)**, and the REST reconciliation |
| `messages.jsonl` | Every WebSocket message exactly as received, with local wall-clock and monotonic receive times and the connection number (reproducible evidence) |
| `rest_klines.json` | The REST klines used for reconciliation |

## How the result will be read
| Outcome | Meaning for D-12 / E-1 |
|---|---|
| `verdict: OBSERVED…`, class `Z1`, every REST zero-trade minute "delivered on WS", no WebSocket/REST mismatches | Live behaves like the archive. The no-fill rule can treat live and backtest zero-trade candles identically |
| `verdict: DIFFERS…`: REST shows zero-trade minutes that the WebSocket did not deliver as closed candles, or they differ | Live differs from the archive. E-1 must define how the live builder and paper handle those minutes before the rule is final |
| `verdict: INCONCLUSIVE…`: no zero-trade minute occurred in the hour (REST shows none either); `UNKNOWN…` if REST was unavailable | Inconclusive. Repeat with thinner symbols from a new preflight |
| `verdict: OBSERVED_UNCONFIRMED…`: Z1 candles on the WebSocket, but REST unavailable or no REST zero-trade minute in the judged window | Supports the archive-like behaviour, but needs confirmation. Repeat if possible |
| `verdict: INCONCLUSIVE_CONNECTION…`: REST zero-trade minutes fell inside connection gaps (`disconnect_windows`) | Explained by the connection, not by Binance. Repeat the capture |
| `verdict: ANOMALOUS…`: zero-trade candles not Z1-shaped (not flat at the previous close, or volume without trades) | Investigate before deciding |
| A zero-trade candle with class `A_no_previous_close` (the first candle of a connection) | Cannot be verified; it does not count either way |

## Send back
- the folder's `summary.json`, and the `messages.jsonl` only if something is unexpected;
- the preflight output you used to choose the symbols;
- the WHEA counts before and after, if taken, and any sleep, crash or network interruption you noticed.

## Results (owner run 2026-10-29T01:07Z UTC)
**Verdict: PASS — `OBSERVED: closed zero-trade candles (Z1) on the WebSocket, confirmed by REST`.** Reconciled
by the Lead against the owner's output folder `evidence/owner_checks/o4_20261029T010705Z/` (kept in place,
untracked).

| Item | Value |
|---|---|
| Tool | `o4_zero_trade_capture` v2, script SHA-256 `c1c35fa3…be34b50`, git HEAD `ebefbc1` (9 tracked files dirty: the uncommitted E-0/E-4(a) work) |
| Environment | Windows 11 10.0.26200, Python 3.13.2, websockets 17.3, httpx 0.28.1; normal PowerShell, no administrator rights |
| Window | 2026-10-29 01:07:05Z → 02:07:21Z (60.27 min, planned 60, not aborted); judged minutes 01:08 → 02:05 (58 per symbol) |
| Symbols | ZRXUSDT, CVCUSDT, OGNUSDT (thin spot pairs from the preflight) |
| Connection | 1 connection, 0 disconnects, 0 stalls (`STALL_S = 120`), closed at the deadline; no errors |
| Closed candles | 60 per symbol; 0 missing closed minutes; 0 duplicates, 0 conflicts |
| Zero-trade candles on WS | ZRX 26 Z1 (+1 `A_no_previous_close`, the first candle of the connection, which cannot be verified and is **not counted**), CVC 24 Z1, OGN 20 Z1: **70 Z1** |
| REST reconciliation | 58 REST minutes per symbol; REST zero-trade minutes 26 / 24 / 20, **all delivered on the WebSocket**; 0 REST-missing, 0 WS-missing versus REST, 0 field mismatches |
| Message gaps | max 57.7 s / 59.4 s / 59.3 s: during zero-trade minutes the stream sends only the closed candle, about once a minute. This validates the 120 s stall threshold (a 30 s threshold would have produced false stalls) |
| Clock | server − local: +5.1 s at start (round trip 9.9 s, so unreliable) and +0.65 s at end (round trip 0.87 s); the local clock runs about 0.65 s behind. Receipt "latencies" are therefore slightly negative (p50 −87 / −157 / −167 ms); event time minus close p50 is 92 / 30 / 18 ms |
| File hashes (SHA-256) | `summary.json` `8fb4931d…f531342d0`, `messages.jsonl` `d81e7d21…af12aad9`, `rest_klines.json` `cfc82c4e…d81567be` |

**Meaning for D-12 / E-1:** live behaves like the archive. One no-fill rule on published candles (`trades == 0`)
serves backtest and paper alike, and the live bar builder needs no change. D-12 is now **approved for
implementation, subject to validation** (owner, 2026-10-28). E-1 implements it; see the
[E-1 devlog](2026-10-28-m2-e1-no-fill-and-interruptions.md).

**Limits of this evidence:** one hour, three spot symbols, one connection. USD-M was not checked. A disconnect
during a zero-trade minute was not exercised; the existing fail-closed unobserved-minute rules cover that case.
