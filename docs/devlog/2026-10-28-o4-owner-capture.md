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
