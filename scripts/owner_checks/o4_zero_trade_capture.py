"""O-4: read-only live capture of Binance spot 1m klines, to check how zero-trade minutes arrive.

Question it answers (ADR-0014 rule 4 / owner decision D-12): does the live kline stream deliver a
*closed* candle for a minute without trades (n = 0, flat at the previous close), like the archive?

Safety by construction:
- only Binance's market-data-only hosts are contacted (`data-stream.binance.vision` WebSocket,
  `data-api.binance.vision` REST): they serve public market data and cannot accept orders; no API
  key, no signed request, no account endpoint exists in this file;
- no `tsclaude` import: it cannot touch the app, the paper runtime, the event store, the lake,
  `data/` or the sealed holdout; requested time windows inside the 2026 holdout are refused;
- it writes only into a new output folder it refuses to overwrite; bounded duration (≤ 120 min).

Commands (normal PowerShell, no administrator):
  python -I scripts/owner_checks/o4_zero_trade_capture.py preflight --symbols HBARUSDT,XYZUSDT
  python -I scripts/owner_checks/o4_zero_trade_capture.py capture --symbols HBARUSDT --minutes 60
         --out-dir evidence/owner_checks/o4_<stamp>
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import contextlib
import hashlib
import json
import platform
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

TOOL = "o4_zero_trade_capture"
TOOL_VERSION = "2"
WS_BASE = "wss://data-stream.binance.vision"
REST_BASE = "https://data-api.binance.vision"
ALLOWED_HOSTS = frozenset({"data-stream.binance.vision", "data-api.binance.vision"})
MINUTE = 60_000
REST_PAGE = 1000  # Binance klines maximum per request
MAX_MINUTES = 120
MAX_SYMBOLS = 5
# No message for this long on an open connection: treat as stalled and reconnect. Longer than one
# candle period, so a quiet market (thin symbol, no trades) is never mistaken for a dead link; the
# WebSocket keepalive pings (20 s) detect a dead connection independently.
STALL_S = 120.0
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
HOLDOUT = (1_767_225_600_000, 1_790_812_800_000)  # [2026-01-01, 2026-10-01) UTC, sealed (ADR-0009)
_SYMBOL = re.compile(r"^[A-Z0-9]{5,20}$")


class CaptureError(RuntimeError):
    """A refused or impossible request (exit code 2)."""


# ---------------------------------------------------------------------- validation and URLs
def parse_symbols(text: str) -> list[str]:
    symbols = [s.strip().upper() for s in text.split(",") if s.strip()]
    if not symbols:
        raise CaptureError("at least one symbol is required")
    if len(symbols) > MAX_SYMBOLS:
        raise CaptureError(f"at most {MAX_SYMBOLS} symbols")
    if len(set(symbols)) != len(symbols):
        raise CaptureError("duplicate symbol")
    for s in symbols:
        if not _SYMBOL.fullmatch(s):
            raise CaptureError(f"invalid symbol {s!r}")
    return symbols


def check_minutes(minutes: int) -> int:
    if not 1 <= minutes <= MAX_MINUTES:
        raise CaptureError(f"--minutes must be 1..{MAX_MINUTES}")
    return minutes


def check_window(start_ms: int, end_ms: int) -> None:
    """Refuse any request that overlaps the sealed 2026 holdout."""
    if start_ms < HOLDOUT[1] and end_ms > HOLDOUT[0]:
        raise CaptureError("requested window overlaps the sealed holdout [2026-01-01, 2026-10-01)")


def check_url(url: str) -> str:
    host = urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise CaptureError(f"host {host!r} is not an allowed market-data host")
    return url


def stream_url(symbols: Iterable[str]) -> str:
    streams = "/".join(f"{s.lower()}@kline_1m" for s in symbols)
    return check_url(f"{WS_BASE}/stream?streams={streams}")


def prepare_out_dir(path: Path) -> Path:
    if path.exists():
        raise CaptureError(f"refusing to overwrite existing {path}")
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------- parsing and tracking
def _ms(value: int) -> int:
    return value // 1000 if value > 10**14 else value  # tolerate microsecond timestamps


@dataclass(frozen=True)
class Kline:
    symbol: str
    open_ms: int
    close_ms: int
    closed: bool
    event_ms: int
    trades: int
    o: str
    h: str
    lo: str
    c: str
    v: str
    q: str
    tv: str
    tq: str

    def values(self) -> tuple[str, ...]:
        return (self.o, self.h, self.lo, self.c, self.v, self.q, self.tv, self.tq, str(self.trades))


def parse_message(raw: str) -> Kline | None:
    """A combined-stream kline message, or None for anything else (subscription acks etc.)."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    data = msg.get("data") if isinstance(msg, dict) else None
    if not isinstance(data, dict) or data.get("e") != "kline":
        return None
    try:
        return _kline(data)
    except (KeyError, TypeError, ValueError):
        return None  # malformed kline payload: kept in messages.jsonl, ignored here


def _kline(data: dict[str, Any]) -> Kline:
    k = data["k"]
    return Kline(
        symbol=str(k["s"]),
        open_ms=_ms(int(k["t"])),
        close_ms=_ms(int(k["T"])),
        closed=bool(k["x"]),
        event_ms=_ms(int(data["E"])),
        trades=int(k["n"]),
        o=str(k["o"]),
        h=str(k["h"]),
        lo=str(k["l"]),
        c=str(k["c"]),
        v=str(k["v"]),
        q=str(k["q"]),
        tv=str(k["V"]),
        tq=str(k["Q"]),
    )


def zero_trade_class(k: Kline, prev_close: str | None) -> str:
    """ADR-0014 classes for a zero-trade candle: Z1 (flat at previous close, all volumes 0) or A."""
    vols_zero = all(Decimal(x) == 0 for x in (k.v, k.q, k.tv, k.tq))
    flat = Decimal(k.o) == Decimal(k.h) == Decimal(k.lo) == Decimal(k.c)
    if prev_close is None:
        return "A_no_previous_close" if (vols_zero and flat) else "A"
    return "Z1" if vols_zero and flat and Decimal(k.o) == Decimal(prev_close) else "A"


@dataclass
class SymbolTrack:
    closed: dict[int, Kline] = field(default_factory=dict)
    receipt_ms: dict[int, int] = field(default_factory=dict)
    duplicates: int = 0
    conflicts: list[int] = field(default_factory=list)
    messages: int = 0
    last_recv_ms: int | None = None
    max_gap_ms: int = 0

    def on_message(self, k: Kline, recv_ms: int) -> None:
        self.messages += 1
        if self.last_recv_ms is not None:
            self.max_gap_ms = max(self.max_gap_ms, recv_ms - self.last_recv_ms)
        self.last_recv_ms = recv_ms
        if not k.closed:
            return
        seen = self.closed.get(k.open_ms)
        if seen is None:
            self.closed[k.open_ms] = k
            self.receipt_ms[k.open_ms] = recv_ms
        elif seen.values() == k.values():
            self.duplicates += 1
        else:
            self.conflicts.append(k.open_ms)

    def missing(self, first_ms: int, last_ms: int) -> list[int]:
        """Minute open times in [first_ms, last_ms] with no closed candle received."""
        return [m for m in range(first_ms, last_ms + 1, MINUTE) if m not in self.closed]


def zero_trade_report(track: SymbolTrack) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in sorted(track.closed):
        k = track.closed[m]
        if k.trades != 0:
            continue
        prev = track.closed.get(m - MINUTE)
        out.append(
            {
                "open": iso(m),
                "class": zero_trade_class(k, prev.c if prev else None),
                "ohlc": [k.o, k.h, k.lo, k.c],
                "prev_close": prev.c if prev else None,
                "receipt_latency_ms": track.receipt_ms[m] - k.close_ms,
            }
        )
    return out


def reconcile(
    track: SymbolTrack, rest_rows: list[list[Any]], first_ms: int, last_ms: int
) -> dict[str, Any]:
    """Compare closed WebSocket candles with REST klines for [first_ms, last_ms]."""
    rest: dict[int, tuple[str, ...]] = {}
    rest_zero: list[int] = []
    for r in rest_rows:
        t = _ms(int(r[0]))
        if not first_ms <= t <= last_ms:
            continue
        rest[t] = (
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            str(r[5]),
            str(r[7]),
            str(r[9]),
            str(r[10]),
            str(int(r[8])),
        )
        if int(r[8]) == 0:
            rest_zero.append(t)
    mismatched = [
        iso(t)
        for t, vals in rest.items()
        if t in track.closed and _norm(track.closed[t].values()) != _norm(vals)
    ]
    return {
        "rest_minutes": len(rest),
        "rest_missing_minutes": [
            iso(t) for t in range(first_ms, last_ms + 1, MINUTE) if t not in rest
        ],
        "ws_missing_vs_rest": [iso(t) for t in sorted(rest) if t not in track.closed],
        "field_mismatches": mismatched,
        "rest_zero_trade_minutes": [iso(t) for t in rest_zero],
        "rest_zero_trade_delivered_on_ws": [iso(t) for t in rest_zero if t in track.closed],
    }


def _norm(vals: tuple[str, ...]) -> tuple[Decimal, ...]:
    return tuple(Decimal(v) for v in vals)


def iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def latency_stats(track: SymbolTrack) -> dict[str, Any]:
    """Receipt latency (local receive - candle close) and exchange event latency (E - close)."""
    lat = sorted(track.receipt_ms[m] - track.closed[m].close_ms for m in track.closed)
    ev = sorted(track.closed[m].event_ms - track.closed[m].close_ms for m in track.closed)
    if not lat:
        return {"n": 0}
    return {
        "n": len(lat),
        "p50_ms": statistics.median(lat),
        "p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
        "max_ms": lat[-1],
        "event_minus_close_p50_ms": statistics.median(ev),
        "event_minus_close_max_ms": ev[-1],
    }


# ---------------------------------------------------------------------- capture loop
class WsConnection(Protocol):
    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], contextlib.AbstractAsyncContextManager[WsConnection]]


@dataclass
class Clock:
    wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000
    mono_s: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class CaptureResult:
    tracks: dict[str, SymbolTrack]
    events: list[dict[str, Any]]
    started_ms: int
    ended_ms: int
    aborted: bool = False
    connections: int = 0


def new_result(symbols: list[str], started_ms: int) -> CaptureResult:
    return CaptureResult({s: SymbolTrack() for s in symbols}, [], started_ms, started_ms)


async def capture(
    symbols: list[str],
    seconds: float,
    connect: Connector,
    write_raw: Callable[[dict[str, Any]], None],
    clock: Clock | None = None,
    stall_s: float = STALL_S,
    result: CaptureResult | None = None,
) -> CaptureResult:
    """Read the combined kline stream until `seconds` have passed, reconnecting with back-off.

    `result` may be supplied by the caller: it is updated in place, so tracks and connection events
    survive a cancellation (Ctrl+C) and can still be reported."""
    clock = clock or Clock()
    url = stream_url(symbols)
    res = result or new_result(symbols, clock.wall_ms())
    tracks = res.tracks
    deadline = clock.mono_s() + seconds
    attempt = 0
    while clock.mono_s() < deadline:
        res.connections += 1
        conn_id = res.connections
        opened = clock.wall_ms()
        reason = "deadline"
        try:
            async with connect(url) as ws:
                res.events.append({"event": "connected", "conn": conn_id, "at": opened})
                attempt = 0
                while True:
                    remaining = deadline - clock.mono_s()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(stall_s, remaining))
                    except TimeoutError:
                        if deadline - clock.mono_s() <= 0:
                            break
                        reason = f"stalled: no message for {stall_s:g} s"
                        raise
                    recv = clock.wall_ms()
                    text = raw.decode() if isinstance(raw, bytes) else raw
                    write_raw(
                        {"recv_ms": recv, "mono_s": clock.mono_s(), "conn": conn_id, "raw": text}
                    )
                    k = parse_message(text)
                    if k is not None and k.symbol in tracks:
                        tracks[k.symbol].on_message(k, recv)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # disconnect, stall, DNS, TLS ...: log, back off, reconnect
            if reason == "deadline":
                reason = f"{type(exc).__name__}: {exc}"[:300]
            res.events.append(
                {"event": "disconnected", "conn": conn_id, "at": clock.wall_ms(), "reason": reason}
            )
            if clock.mono_s() >= deadline:
                break
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            res.events.append({"event": "backoff", "seconds": delay, "at": clock.wall_ms()})
            await clock.sleep(min(delay, max(0.0, deadline - clock.mono_s())))
            continue
        res.events.append(
            {"event": "closed", "conn": conn_id, "at": clock.wall_ms(), "reason": reason}
        )
    res.ended_ms = clock.wall_ms()
    return res


def disconnect_windows(events: list[dict[str, Any]], ended_ms: int) -> list[tuple[int, int]]:
    """[disconnected, next connected) intervals, from the connection events."""
    windows: list[tuple[int, int]] = []
    down: int | None = None
    for e in events:
        if e["event"] in ("disconnected", "aborted_by_user") and down is None:
            down = int(e["at"])
        elif e["event"] == "connected" and down is not None:
            windows.append((down, int(e["at"])))
            down = None
    if down is not None:
        windows.append((down, ended_ms))
    return windows


def _from_iso(text: str) -> int:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) * 1000


def verdict(
    per: dict[str, Any], rest_available: bool, windows: list[tuple[int, int]] | None = None
) -> str:
    """One-line answer to the O-4 question, across all symbols (see the O-4 devlog table).

    A REST zero-trade minute missing on the WebSocket counts against the stream only if its close
    did not fall inside a connection gap; only Z1-shaped candles count as observed zero-trade."""
    windows = windows or []
    margin = 5_000  # the closed candle arrives a few seconds after the close

    def in_gap(open_iso: str) -> bool:
        close = _from_iso(open_iso) + MINUTE - 1
        return any(lo - margin <= close < hi + margin for lo, hi in windows)

    differs = during_gap = False
    for s in per.values():
        rec = s.get("rest_reconciliation", {})
        missing = set(rec.get("rest_zero_trade_minutes", [])) - set(
            rec.get("rest_zero_trade_delivered_on_ws", [])
        )
        for m in missing:
            if in_gap(m):
                during_gap = True
            else:
                differs = True
    classes = [z["class"] for s in per.values() for z in s["zero_trade_closed_candles"]]
    rest_zero = any(
        s.get("rest_reconciliation", {}).get("rest_zero_trade_minutes") for s in per.values()
    )
    if differs:
        return (
            "DIFFERS: REST has zero-trade minutes the WebSocket did not deliver as closed candles"
        )
    if "Z1" in classes:
        if rest_available and rest_zero:
            return "OBSERVED: closed zero-trade candles (Z1) on the WebSocket, confirmed by REST"
        return "OBSERVED_UNCONFIRMED: Z1 zero-trade candles on the WebSocket, not confirmed by REST"
    if "A" in classes:  # A_no_previous_close is unverifiable, not anomalous
        return "ANOMALOUS: zero-trade candles on the WebSocket, none of them Z1-shaped"
    if during_gap:
        return "INCONCLUSIVE_CONNECTION: REST zero-trade minutes fell inside connection gaps"
    if not rest_available:
        return "UNKNOWN: no zero-trade candle on the WebSocket and REST reconciliation unavailable"
    return (
        "INCONCLUSIVE: no zero-trade minute occurred in the judged window; "
        "repeat on thinner symbols"
    )


def summarize(res: CaptureResult, rest: dict[str, list[list[Any]]] | None) -> dict[str, Any]:
    """Per-symbol evidence. Only minutes that were fully inside the capture window are judged."""
    first = res.started_ms - res.started_ms % MINUTE + MINUTE
    last = (
        res.ended_ms - res.ended_ms % MINUTE - 2 * MINUTE
    )  # last minute surely closed and delivered
    per: dict[str, Any] = {}
    for sym, tr in res.tracks.items():
        entry: dict[str, Any] = {
            "messages": tr.messages,
            "closed_candles": len(tr.closed),
            "judged_minutes": max(0, (last - first) // MINUTE + 1),
            "missing_closed_minutes": [iso(m) for m in tr.missing(first, last)]
            if last >= first
            else [],
            "duplicates_identical": tr.duplicates,
            "duplicate_conflicts": [iso(m) for m in tr.conflicts],
            "max_message_gap_ms": tr.max_gap_ms,
            "receipt_latency": latency_stats(tr),
            "zero_trade_closed_candles": zero_trade_report(tr),
        }
        if rest is not None and last >= first:
            entry["rest_reconciliation"] = reconcile(tr, rest.get(sym, []), first, last)
        per[sym] = entry
    disconnects = [e for e in res.events if e["event"] == "disconnected"]
    return {
        "judged_window": {"first_open": iso(first), "last_open": iso(last)}
        if last >= first
        else None,
        "connections": res.connections,
        "disconnects": len(disconnects),
        "stalls": sum(1 for e in disconnects if str(e.get("reason", "")).startswith("stalled")),
        "symbols": per,
        "zero_trade_candles_observed_on_ws": any(per[s]["zero_trade_closed_candles"] for s in per),
        "disconnect_windows": [
            {"from": iso(lo), "to": iso(hi)}
            for lo, hi in disconnect_windows(res.events, res.ended_ms)
        ],
        "verdict": verdict(per, rest is not None, disconnect_windows(res.events, res.ended_ms)),
    }


# ---------------------------------------------------------------------- REST (read-only)
def fetch_klines(client: Any, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    """Public klines, paginated (≤ 1000 per call). `client` is an httpx.Client."""
    check_window(start_ms, end_ms + MINUTE)  # Binance `endTime` includes the kline opening at it
    url = check_url(f"{REST_BASE}/api/v3/klines")
    rows: list[list[Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        resp = client.get(
            url,
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": REST_PAGE,
            },
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        cursor = _ms(int(page[-1][0])) + MINUTE
        if len(page) < REST_PAGE:
            break
    return rows


def server_offset_ms(client: Any, clock: Clock) -> dict[str, int]:
    before = clock.wall_ms()
    resp = client.get(check_url(f"{REST_BASE}/api/v3/time"))
    after = clock.wall_ms()
    resp.raise_for_status()
    server = int(resp.json()["serverTime"])
    return {
        "server_minus_local_ms": server - (before + after) // 2,
        "round_trip_ms": after - before,
    }


# ---------------------------------------------------------------------- provenance
def provenance() -> dict[str, Any]:
    script = Path(__file__).resolve()
    meta: dict[str, Any] = {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        import httpx
        import websockets

        meta["websockets"] = getattr(websockets, "__version__", "unknown")
        meta["httpx"] = httpx.__version__
    except ImportError:  # pragma: no cover - reported, not fatal
        meta["libraries"] = "unavailable"
    try:
        root = script.parents[2]
        meta["git_head"] = subprocess.run(  # fixed argv, read-only
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        dirty = subprocess.run(  # fixed argv, read-only
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        meta["git_dirty_tracked_files"] = len([line for line in dirty.splitlines() if line.strip()])
    except (OSError, subprocess.SubprocessError):
        meta["git_head"] = None
    return meta


# ---------------------------------------------------------------------- commands
def cmd_preflight(symbols: list[str], client: Any | None = None) -> int:
    """Zero-trade minutes over the last 24 h per symbol (REST, read-only), to choose symbols."""
    import httpx

    clock = Clock()
    end = clock.wall_ms() - clock.wall_ms() % MINUTE - MINUTE
    start = end - 1439 * MINUTE
    http = client or httpx.Client(timeout=10.0, headers={"User-Agent": f"{TOOL}/{TOOL_VERSION}"})
    with http as client:
        offset = server_offset_ms(client, clock)
        print(
            f"clock: server - local = {offset['server_minus_local_ms']} ms "
            f"(round trip {offset['round_trip_ms']} ms)"
        )
        for sym in symbols:
            try:
                rows = fetch_klines(client, sym, start, end)
            except Exception as exc:  # unknown or delisted symbol, HTTP error: report, continue
                print(f"{sym}: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
                continue
            zero = sum(1 for r in rows if int(r[8]) == 0)
            per_hour = zero / 24 if rows else 0.0
            print(
                f"{sym}: {len(rows)} minutes in the last 24 h, "
                f"zero-trade minutes {zero} (~{per_hour:.1f} per hour)"
            )
    print(
        "Choose symbols with several zero-trade minutes per hour; up to 5 can be captured together."
    )
    return 0


def check_out_dir(out_dir: Path, allowed_root: Path) -> Path:
    """The output folder must be new and inside `evidence/owner_checks/` (never data or lake)."""
    target = out_dir.resolve()
    root = allowed_root.resolve()
    if target == root or root not in target.parents:
        raise CaptureError(f"--out-dir must be a new folder inside {root}")
    if target.exists():
        raise CaptureError(f"refusing to overwrite existing {target}")
    return target


def cmd_capture(
    symbols: list[str],
    minutes: int,
    out_dir: Path,
    *,
    _seconds: float | None = None,
    _out_root: Path | None = None,
) -> int:
    """Bounded capture plus REST reconciliation (`_seconds`, `_out_root`: offline tests only).

    The report is always written once the capture has started: after the full run, after Ctrl+C,
    and when the final REST reconciliation fails (the error is recorded; reconciliation omitted)."""
    import httpx
    from websockets.asyncio.client import connect

    clock = Clock()
    start = clock.wall_ms()
    check_window(start - MINUTE, start + (minutes + 5) * MINUTE)
    allowed = _out_root or Path(__file__).resolve().parents[2] / "evidence" / "owner_checks"
    target = check_out_dir(out_dir, allowed)
    meta = provenance() | {
        "command": "capture",
        "symbols": symbols,
        "planned_minutes": minutes,
        "ws_url": stream_url(symbols),
        "rest_base": REST_BASE,
        "stall_s": STALL_S,
        "started_utc": iso(start),
    }
    errors: list[str] = []
    with httpx.Client(timeout=10.0, headers={"User-Agent": f"{TOOL}/{TOOL_VERSION}"}) as client:
        try:
            meta["clock_start"] = server_offset_ms(client, clock)
        except Exception as exc:  # nothing captured yet: refuse cleanly, leave no folder behind
            raise CaptureError(f"cannot reach {REST_BASE}: {type(exc).__name__}: {exc}") from exc
        out = prepare_out_dir(target)
        raw_path = out / "messages.jsonl"
        print(
            f"capturing {symbols} for {minutes} min into {out} "
            "(Ctrl+C stops early and still writes the report)"
        )

        def ws_connect(url: str) -> Any:
            return connect(
                check_url(url), open_timeout=10, ping_interval=20, ping_timeout=20, max_size=2**20
            )

        res = new_result(symbols, start)
        with raw_path.open("x", encoding="utf-8") as raw:

            def write_raw(rec: dict[str, Any]) -> None:
                raw.write(json.dumps(rec, separators=(",", ":")) + "\n")

            seconds = minutes * 60.0 if _seconds is None else _seconds
            try:
                asyncio.run(capture(symbols, seconds, ws_connect, write_raw, clock, result=res))
            except KeyboardInterrupt:
                res.aborted = True
                res.ended_ms = clock.wall_ms()
                res.events.append({"event": "aborted_by_user", "at": res.ended_ms})
            except Exception as exc:  # unexpected: keep what was captured and say why it stopped
                res.ended_ms = clock.wall_ms()
                errors.append(f"capture stopped: {type(exc).__name__}: {exc}"[:300])
        rest: dict[str, list[list[Any]]] | None = None
        try:
            meta["clock_end"] = server_offset_ms(client, clock)
            first = res.started_ms - res.started_ms % MINUTE
            fetched = {sym: fetch_klines(client, sym, first, res.ended_ms) for sym in symbols}
            rest = fetched
        except (Exception, KeyboardInterrupt) as exc:  # report anyway, without reconciliation
            errors.append(f"REST reconciliation unavailable: {type(exc).__name__}: {exc}"[:300])
            rest = None
    summary = summarize(res, rest)
    report = (
        meta
        | {
            "ended_utc": iso(res.ended_ms),
            "actual_minutes": round((res.ended_ms - res.started_ms) / MINUTE, 2),
            "aborted": res.aborted,
            "errors": errors,
            "connection_events": res.events,
        }
        | summary
    )
    if rest is not None:
        (out / "rest_klines.json").write_text(json.dumps(rest), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print_summary(report, out)
    return 0


def _print_summary(report: dict[str, Any], out: Path) -> None:
    print(
        f"done: {report['actual_minutes']} min, connections {report['connections']}, "
        f"disconnects {report['disconnects']}, stalls {report['stalls']}, "
        f"aborted {report['aborted']}"
    )
    for sym, s in report["symbols"].items():
        rec = s.get("rest_reconciliation", {})
        print(
            f"{sym}: closed {s['closed_candles']}/{s['judged_minutes']} judged minutes, "
            f"missing {len(s['missing_closed_minutes'])}, "
            f"zero-trade closed on WS {len(s['zero_trade_closed_candles'])}, "
            f"REST zero-trade {len(rec.get('rest_zero_trade_minutes', []))} "
            f"(delivered on WS {len(rec.get('rest_zero_trade_delivered_on_ws', []))}), "
            f"WS vs REST mismatches {len(rec.get('field_mismatches', []))}"
        )
    for err in report.get("errors", []):
        print(f"note: {err}")
    print(f"verdict: {report['verdict']}")
    print(f"report: {out / 'summary.json'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight", help="zero-trade minutes in the last 24 h (REST, read-only)")
    pre.add_argument("--symbols", required=True)
    cap = sub.add_parser("capture", help="bounded live capture plus REST reconciliation")
    cap.add_argument("--symbols", required=True)
    cap.add_argument("--minutes", type=int, default=60)
    cap.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        symbols = parse_symbols(args.symbols)
        if args.command == "preflight":
            return cmd_preflight(symbols)
        return cmd_capture(symbols, check_minutes(args.minutes), Path(args.out_dir))
    except CaptureError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
