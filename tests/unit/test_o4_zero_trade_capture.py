"""`scripts/owner_checks/o4_zero_trade_capture.py` (O-4): offline tests only (no network).

The capture loop runs against a fake WebSocket connector; REST is not called."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "owner_checks" / "o4_zero_trade_capture.py"
)
T0 = 1_792_000_020_000 - 1_792_000_020_000 % 60_000  # after the sealed holdout (2026-10)
M = 60_000


@pytest.fixture(scope="module")
def o4() -> ModuleType:
    spec = importlib.util.spec_from_file_location("o4_capture", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def kline_msg(
    open_ms: int,
    *,
    symbol: str = "HBARUSDT",
    closed: bool = True,
    n: int = 5,
    o: str = "0.1",
    h: str = "0.1",
    lo: str = "0.1",
    c: str = "0.1",
    v: str = "10",
) -> str:
    q = "0" if v == "0" else "1"
    k = {
        "t": open_ms, "T": open_ms + M - 1, "s": symbol, "i": "1m", "x": closed, "n": n,
        "o": o, "h": h, "l": lo, "c": c, "v": v, "q": q, "V": "0" if v == "0" else "5",
        "Q": "0" if v == "0" else "0.5",
    }  # fmt: skip
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@kline_1m",
            "data": {"e": "kline", "E": open_ms + M, "s": symbol, "k": k},
        }
    )


def rest_row(
    open_ms: int, *, n: int = 5, o: str = "0.1", c: str = "0.1", v: str = "10"
) -> list[Any]:
    q = "0" if v == "0" else "1"
    tv, tq = ("0", "0") if v == "0" else ("5", "0.5")
    return [open_ms, o, o, o, c, v, open_ms + M - 1, q, n, tv, tq, "0"]


# ---------------------------------------------------------------------- parsing and classification
def test_parse_message_reads_closed_klines_and_ignores_everything_else(o4: ModuleType) -> None:
    k = o4.parse_message(kline_msg(T0, n=0, v="0"))
    assert (k.symbol, k.open_ms, k.closed, k.trades) == ("HBARUSDT", T0, True, 0)
    assert o4.parse_message('{"result": null, "id": 1}') is None
    assert o4.parse_message("not json") is None
    micro = json.loads(kline_msg(T0))
    micro["data"]["k"]["t"] *= 1000
    micro["data"]["k"]["T"] = micro["data"]["k"]["T"] * 1000 + 999
    assert o4.parse_message(json.dumps(micro)).open_ms == T0  # microseconds tolerated


def test_zero_trade_candles_are_classified_like_adr_0014(o4: ModuleType) -> None:
    flat = o4.parse_message(kline_msg(T0, n=0, v="0", o="0.1", h="0.1", lo="0.1", c="0.1"))
    assert o4.zero_trade_class(flat, "0.1") == "Z1"
    assert o4.zero_trade_class(flat, "0.1000") == "Z1"  # numeric, not textual, comparison
    assert o4.zero_trade_class(flat, "0.2") == "A"  # flat, but not at the previous close
    assert o4.zero_trade_class(flat, None) == "A_no_previous_close"
    moving = o4.parse_message(kline_msg(T0, n=0, v="0", h="0.2"))
    assert o4.zero_trade_class(moving, "0.1") == "A"
    volume = o4.parse_message(kline_msg(T0, n=0, v="3"))
    assert o4.zero_trade_class(volume, "0.1") == "A"


def test_tracking_detects_missing_duplicate_and_conflicting_closed_candles(o4: ModuleType) -> None:
    tr = o4.SymbolTrack()
    tr.on_message(o4.parse_message(kline_msg(T0, closed=False)), T0 + 1_000)
    tr.on_message(o4.parse_message(kline_msg(T0)), T0 + M + 300)
    tr.on_message(o4.parse_message(kline_msg(T0)), T0 + M + 900)  # identical repeat
    tr.on_message(o4.parse_message(kline_msg(T0 + 2 * M, c="0.2")), T0 + 3 * M + 200)
    tr.on_message(o4.parse_message(kline_msg(T0 + 2 * M, c="0.3")), T0 + 3 * M + 400)  # conflict
    assert tr.messages == 5
    assert tr.duplicates == 1
    assert tr.conflicts == [T0 + 2 * M]
    assert tr.missing(T0, T0 + 2 * M) == [T0 + M]
    assert tr.max_gap_ms == 2 * M - 700  # between T0+M+900 and T0+3M+200
    assert tr.receipt_ms[T0] == T0 + M + 300  # first receipt kept


def test_rest_reconciliation_reports_gaps_mismatches_and_zero_trade_delivery(
    o4: ModuleType,
) -> None:
    tr = o4.SymbolTrack()
    tr.on_message(o4.parse_message(kline_msg(T0)), T0 + M)
    tr.on_message(o4.parse_message(kline_msg(T0 + M, n=0, v="0")), T0 + 2 * M)
    tr.on_message(o4.parse_message(kline_msg(T0 + 3 * M, c="0.2")), T0 + 4 * M)
    rows = [
        rest_row(T0),
        rest_row(T0 + M, n=0, v="0"),
        rest_row(T0 + 2 * M),
        rest_row(T0 + 3 * M, c="0.3"),
    ]
    rep = o4.reconcile(tr, rows, T0, T0 + 3 * M)
    assert rep["rest_minutes"] == 4
    assert rep["ws_missing_vs_rest"] == [o4.iso(T0 + 2 * M)]
    assert rep["field_mismatches"] == [o4.iso(T0 + 3 * M)]
    assert rep["rest_zero_trade_minutes"] == [o4.iso(T0 + M)]
    assert rep["rest_zero_trade_delivered_on_ws"] == [o4.iso(T0 + M)]
    assert rep["rest_missing_minutes"] == []


# ---------------------------------------------------------------------- safety
def test_inputs_are_validated_and_bounded(o4: ModuleType, tmp_path: Path) -> None:
    assert o4.parse_symbols("hbarusdt, ARDRUSDT") == ["HBARUSDT", "ARDRUSDT"]
    for bad in (
        "",
        "A,B,C,D,E,F".replace(",", "USDT,") + "USDT",
        "HBARUSDT,HBARUSDT",
        "HBAR/USDT",
        "X",
    ):
        with pytest.raises(o4.CaptureError):
            o4.parse_symbols(bad)
    assert o4.check_minutes(60) == 60
    for bad in (0, 121, -5):
        with pytest.raises(o4.CaptureError):
            o4.check_minutes(bad)
    existing = tmp_path / "out"
    existing.mkdir()
    with pytest.raises(o4.CaptureError, match="overwrite"):
        o4.prepare_out_dir(existing)
    assert o4.prepare_out_dir(tmp_path / "new").is_dir()


def test_the_sealed_holdout_window_is_refused(o4: ModuleType) -> None:
    holdout_start, holdout_end = o4.HOLDOUT
    for lo, hi in (
        (holdout_start, holdout_start + M),
        (holdout_end - M, holdout_end + M),
        (0, holdout_end + M),
    ):
        with pytest.raises(o4.CaptureError, match="holdout"):
            o4.check_window(lo, hi)
    o4.check_window(holdout_end, holdout_end + 60 * M)  # after the holdout: allowed
    o4.check_window(holdout_start - 60 * M, holdout_start)  # before it: allowed (end exclusive)


def test_only_market_data_hosts_are_reachable(o4: ModuleType) -> None:
    assert o4.stream_url(["HBARUSDT", "ARDRUSDT"]) == (
        "wss://data-stream.binance.vision/stream?streams=hbarusdt@kline_1m/ardrusdt@kline_1m"
    )
    for url in (
        "https://api.binance.com/api/v3/order",
        "wss://stream.binance.com:9443/ws",
        "https://fapi.binance.com/fapi/v1/order",
        "http://127.0.0.1:8000/api/paper/status",
    ):
        with pytest.raises(o4.CaptureError):
            o4.check_url(url)


def test_the_tool_has_no_order_key_account_or_app_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", source)  # docstrings may describe what is absent
    for forbidden in (
        "X-MBX-APIKEY",
        "signature",
        "/order",
        "/account",
        "listenKey",
        "api_key",
        "secret",
    ):
        assert forbidden.lower() not in code.lower(), forbidden
    assert not re.search(r"^\s*(from|import)\s+tsclaude", code, re.MULTILINE)
    hosts = set(re.findall(r"(?:https?|wss?)://([A-Za-z0-9.-]+)", code))
    assert hosts == {"data-stream.binance.vision", "data-api.binance.vision"}
    for method in (".post(", ".put(", ".delete(", ".patch("):
        assert method not in code, method


# ---------------------------------------------------------------------- capture loop (fake WS)
class FakeWs:
    def __init__(self, script: list[str | BaseException], block: bool) -> None:
        self.script = list(script)
        self.block = block

    async def recv(self) -> str:
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            await asyncio.sleep(0)
            return item
        if self.block:
            await asyncio.sleep(3600)
        raise ConnectionError("server closed")


def fake_connector(sessions: list[FakeWs]) -> Any:
    urls: list[str] = []

    @contextlib.asynccontextmanager
    async def connect(url: str) -> AsyncIterator[FakeWs]:
        urls.append(url)
        if not sessions:
            raise OSError("no more sessions")
        yield sessions.pop(0)

    connect.urls = urls  # type: ignore[attr-defined]
    return connect


def fast_clock(o4: ModuleType) -> Any:
    async def no_sleep(_s: float) -> None:
        await asyncio.sleep(0)

    return o4.Clock(sleep=no_sleep)


def test_capture_logs_disconnects_reconnects_and_keeps_every_message(o4: ModuleType) -> None:
    first = FakeWs(
        [kline_msg(T0), kline_msg(T0 + M), ConnectionError("reset by peer")], block=False
    )
    second = FakeWs([kline_msg(T0 + M), kline_msg(T0 + 2 * M, n=0, v="0")], block=True)
    connect = fake_connector([first, second])
    raw: list[dict[str, Any]] = []
    res = asyncio.run(
        o4.capture(["HBARUSDT"], 0.3, connect, raw.append, fast_clock(o4), stall_s=10.0)
    )
    assert res.connections == 2
    kinds = [e["event"] for e in res.events]
    assert kinds[:4] == ["connected", "disconnected", "backoff", "connected"]
    assert "reset by peer" in res.events[1]["reason"]
    tr = res.tracks["HBARUSDT"]
    assert sorted(tr.closed) == [T0, T0 + M, T0 + 2 * M]
    assert tr.duplicates == 1  # T0+M re-sent after the reconnect
    assert len(raw) == 4
    assert all({"recv_ms", "mono_s", "conn", "raw"} <= set(r) for r in raw)
    assert connect.urls == [o4.stream_url(["HBARUSDT"])] * 2


def test_a_silent_connection_is_treated_as_stalled_and_replaced(o4: ModuleType) -> None:
    sessions = [FakeWs([], block=True) for _ in range(20)]
    res = asyncio.run(
        o4.capture(
            ["HBARUSDT"],
            0.35,
            fake_connector(sessions),
            lambda r: None,
            fast_clock(o4),
            stall_s=0.1,
        )
    )
    stalls = [e for e in res.events if e["event"] == "disconnected"]
    assert stalls
    assert all(e["reason"].startswith("stalled") for e in stalls)
    assert res.connections >= 2
    summary = o4.summarize(res, None)
    assert summary["stalls"] == len(stalls)


def test_summary_judges_only_fully_captured_minutes_and_flags_zero_trade(o4: ModuleType) -> None:
    tracks = {"HBARUSDT": o4.SymbolTrack()}
    tr = tracks["HBARUSDT"]
    for i, n in enumerate([4, 0, 3, 2, 6]):
        tr.on_message(
            o4.parse_message(kline_msg(T0 + i * M, n=n, v="0" if n == 0 else "10")),
            T0 + (i + 1) * M + 500,
        )
    res = o4.CaptureResult(tracks, [], T0 - 30_000, T0 + 7 * M + 10_000)  # started mid-minute
    rest = {
        "HBARUSDT": [
            rest_row(T0 + i * M, n=n, v="0" if n == 0 else "10")
            for i, n in enumerate([4, 0, 3, 2, 6])
        ]
    }
    s = o4.summarize(res, rest)
    assert s["judged_window"] == {"first_open": o4.iso(T0), "last_open": o4.iso(T0 + 5 * M)}
    sym = s["symbols"]["HBARUSDT"]
    assert sym["judged_minutes"] == 6
    assert sym["missing_closed_minutes"] == [o4.iso(T0 + 5 * M)]
    zero = sym["zero_trade_closed_candles"]
    assert [z["open"] for z in zero] == [o4.iso(T0 + M)]
    assert zero[0]["class"] == "Z1"
    assert zero[0]["receipt_latency_ms"] == 501
    assert s["zero_trade_candles_observed_on_ws"] is True
    assert sym["rest_reconciliation"]["rest_zero_trade_delivered_on_ws"] == [o4.iso(T0 + M)]


def test_cli_refusals_exit_2_without_network(o4: ModuleType, tmp_path: Path) -> None:
    assert o4.main(["capture", "--symbols", "HBAR/USDT", "--out-dir", str(tmp_path / "x")]) == 2
    assert (
        o4.main(
            [
                "capture",
                "--symbols",
                "HBARUSDT",
                "--minutes",
                "500",
                "--out-dir",
                str(tmp_path / "x"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "x").exists()


# ---------------------------------------------------------------------- REST and full command
class FakeResponse:
    def __init__(self, body: Any) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.body


class FakeHttp:
    """Stands in for httpx.Client: serves /api/v3/time and paginated /api/v3/klines."""

    def __init__(self, rows: dict[str, list[list[Any]]], page: int = 1000) -> None:
        self.rows = rows
        self.page = page
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> FakeHttp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
        self.calls.append((url, dict(params or {})))
        if url.endswith("/api/v3/time"):
            return FakeResponse({"serverTime": int(__import__("time").time() * 1000)})
        p = params or {}
        rows = [r for r in self.rows.get(p["symbol"], []) if p["startTime"] <= r[0] <= p["endTime"]]
        return FakeResponse(rows[: min(self.page, int(p["limit"]))])


def test_rest_klines_are_paginated_from_the_market_data_host_only(
    o4: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(o4, "REST_PAGE", 2)
    http = FakeHttp({"HBARUSDT": [rest_row(T0 + i * M) for i in range(5)]})
    rows = o4.fetch_klines(http, "HBARUSDT", T0, T0 + 4 * M)
    assert [r[0] for r in rows] == [T0 + i * M for i in range(5)]
    assert all(url == "https://data-api.binance.vision/api/v3/klines" for url, _ in http.calls)
    assert len(http.calls) == 3  # pages of 2, 2 and 1
    with pytest.raises(o4.CaptureError, match="holdout"):
        o4.fetch_klines(http, "HBARUSDT", o4.HOLDOUT[0], o4.HOLDOUT[0] + M)


def test_capture_command_writes_a_reproducible_report_offline(
    o4: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    import httpx
    import websockets.asyncio.client as ws_client

    now = int(_time.time() * 1000)
    base = now - now % M - 3 * M
    msgs = [kline_msg(base), kline_msg(base + M, n=0, v="0"), kline_msg(base + 2 * M)]
    rest = {"HBARUSDT": [rest_row(base), rest_row(base + M, n=0, v="0"), rest_row(base + 2 * M)]}
    seen: dict[str, Any] = {}

    def fake_connect(url: str, **kwargs: Any) -> Any:
        seen["url"], seen["kwargs"] = url, kwargs

        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[FakeWs]:
            yield FakeWs(list(msgs), block=True)

        return cm()

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeHttp(rest))
    monkeypatch.setattr(ws_client, "connect", fake_connect)
    out = tmp_path / "o4_run"
    assert o4.cmd_capture(["HBARUSDT"], 1, out, _seconds=0.3, _out_root=tmp_path) == 0
    assert seen["url"] == "wss://data-stream.binance.vision/stream?streams=hbarusdt@kline_1m"
    assert seen["kwargs"]["ping_interval"] == 20
    report = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    keys = (
        "tool", "tool_version", "script_sha256", "python", "symbols", "ws_url", "started_utc",
        "ended_utc", "clock_start", "clock_end", "connection_events", "aborted",
    )  # fmt: skip
    for key in keys:
        assert key in report, key
    assert report["aborted"] is False
    assert report["planned_minutes"] == 1
    assert len((out / "messages.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert "HBARUSDT" in json.loads((out / "rest_klines.json").read_text(encoding="utf-8"))
    with pytest.raises(
        o4.CaptureError, match="overwrite"
    ):  # a second run never overwrites evidence
        o4.cmd_capture(["HBARUSDT"], 1, out, _seconds=0.1, _out_root=tmp_path)


def test_preflight_reports_zero_trade_rates_and_survives_unknown_symbols(
    o4: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    import time as _time

    now = int(_time.time() * 1000)
    end = now - now % M - M
    rows = [
        rest_row(end - i * M, n=0 if i % 60 == 0 else 3) for i in reversed(range(1440))
    ]  # ascending, as Binance serves them

    class Http(FakeHttp):
        def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
            if params and params.get("symbol") == "NOPEUSDT":
                raise RuntimeError("HTTP 400 invalid symbol")
            return super().get(url, params)

    assert o4.cmd_preflight(["HBARUSDT", "NOPEUSDT"], client=Http({"HBARUSDT": rows})) == 0
    out = capsys.readouterr().out
    assert "HBARUSDT: 1440 minutes in the last 24 h, zero-trade minutes 24 (~1.0 per hour)" in out
    assert "NOPEUSDT: unavailable (RuntimeError: HTTP 400 invalid symbol)" in out


# ---------------------------------------------------------------------- review fixes (B1-B3, N1-N6)
def _run_capture(
    o4: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[FakeWs],
    http: Any,
) -> dict[str, Any]:
    import httpx
    import websockets.asyncio.client as ws_client

    def fake_connect(url: str, **kwargs: Any) -> Any:
        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[FakeWs]:
            if not sessions:
                raise OSError("no more sessions")
            yield sessions.pop(0)

        return cm()

    async def no_sleep(_s: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(ws_client, "connect", fake_connect)
    out = tmp_path / "run"
    assert o4.cmd_capture(["HBARUSDT"], 1, out, _seconds=0.5, _out_root=tmp_path) == 0
    return dict(json.loads((out / "summary.json").read_text(encoding="utf-8")))


def test_stall_timeout_is_longer_than_one_candle(o4: ModuleType) -> None:
    assert o4.STALL_S >= 90  # a quiet thin market must not be mistaken for a dead connection


def test_ctrl_c_keeps_connection_events_and_captured_candles(
    o4: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(o4, "BACKOFF_S", (0.0,))
    first = FakeWs([kline_msg(T0), ConnectionError("reset by peer")], block=False)
    second = FakeWs([kline_msg(T0 + M), KeyboardInterrupt()], block=True)
    report = _run_capture(o4, tmp_path, monkeypatch, [first, second], FakeHttp({}))
    assert report["aborted"] is True
    assert report["disconnects"] == 1  # the disconnect before the abort is still reported
    kinds = [e["event"] for e in report["connection_events"]]
    assert kinds[:3] == ["connected", "disconnected", "backoff"]
    assert kinds[-1] == "aborted_by_user"
    assert report["symbols"]["HBARUSDT"]["closed_candles"] == 2


def test_a_failed_final_rest_call_still_writes_the_report(
    o4: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Flaky(FakeHttp):
        def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
            if url.endswith("/api/v3/klines"):
                raise ConnectionError("network down")
            return super().get(url, params)

    report = _run_capture(
        o4,
        tmp_path,
        monkeypatch,
        [
            FakeWs([kline_msg(T0 - M), kline_msg(T0, n=0, v="0")], block=True)
        ],  # Z1 after a traded candle
        Flaky({}),
    )
    assert any("REST reconciliation unavailable" in e for e in report["errors"])
    assert not (tmp_path / "run" / "rest_klines.json").exists()
    assert report["verdict"].startswith(
        "OBSERVED"
    )  # WS saw a zero-trade candle; REST not available


def test_unreachable_rest_at_start_refuses_without_leaving_a_folder(
    o4: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    class Down(FakeHttp):
        def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
            raise ConnectionError("no route")

    monkeypatch.setattr(httpx, "Client", lambda **kw: Down({}))
    with pytest.raises(o4.CaptureError, match="cannot reach"):
        o4.cmd_capture(["HBARUSDT"], 1, tmp_path / "run", _seconds=0.1, _out_root=tmp_path)
    assert not (tmp_path / "run").exists()


def test_output_must_be_a_new_folder_inside_the_owner_checks_root(
    o4: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "evidence" / "owner_checks"
    root.mkdir(parents=True)
    assert o4.check_out_dir(root / "o4_x", root) == (root / "o4_x").resolve()
    for bad in (tmp_path / "data" / "o4", root, root / ".." / ".." / "data_batches" / "x"):
        with pytest.raises(o4.CaptureError):
            o4.check_out_dir(bad, root)
    (root / "exists").mkdir()
    with pytest.raises(o4.CaptureError, match="overwrite"):
        o4.check_out_dir(root / "exists", root)
    default = o4.Path(o4.__file__).resolve().parents[2] / "evidence" / "owner_checks"
    with pytest.raises(o4.CaptureError):
        o4.check_out_dir(o4.Path(o4.__file__).resolve().parents[2] / "data" / "o4", default)


def test_malformed_kline_payloads_are_ignored(o4: ModuleType) -> None:
    broken = json.loads(kline_msg(T0))
    del broken["data"]["k"]["n"]
    assert o4.parse_message(json.dumps(broken)) is None
    broken["data"]["k"] = "not a dict"
    assert o4.parse_message(json.dumps(broken)) is None


def test_rest_end_time_is_inclusive_at_the_holdout_boundary(o4: ModuleType) -> None:
    http = FakeHttp({})
    with pytest.raises(o4.CaptureError, match="holdout"):
        o4.fetch_klines(http, "HBARUSDT", o4.HOLDOUT[0] - 60 * M, o4.HOLDOUT[0])
    assert http.calls == []  # refused before any request


def test_verdict_distinguishes_every_outcome(o4: ModuleType) -> None:
    t1, t2 = o4.iso(T0), o4.iso(T0 + 5 * M)

    def sym(
        zero: list[tuple[str, str]], rest_zero: list[str] | None, delivered: list[str]
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "zero_trade_closed_candles": [{"open": o, "class": c} for o, c in zero]
        }
        if rest_zero is not None:
            entry["rest_reconciliation"] = {
                "rest_zero_trade_minutes": rest_zero,
                "rest_zero_trade_delivered_on_ws": delivered,
            }
        return entry

    assert o4.verdict({"A": sym([(t1, "Z1")], [t1], [t1])}, True).startswith("OBSERVED:")
    assert o4.verdict({"A": sym([(t1, "Z1")], None, [])}, False).startswith("OBSERVED_UNCONFIRMED")
    assert o4.verdict({"A": sym([(t1, "Z1")], [], [])}, True).startswith("OBSERVED_UNCONFIRMED")
    assert o4.verdict({"A": sym([(t1, "A")], [t1], [t1])}, True).startswith("ANOMALOUS")
    unverifiable = sym([(t1, "A_no_previous_close")], [], [])
    assert o4.verdict({"A": unverifiable}, True).startswith("INCONCLUSIVE:")
    assert o4.verdict({"A": sym([], [t2], [])}, True).startswith("DIFFERS")
    gap = [(T0 + 5 * M + 30_000, T0 + 6 * M + 10_000)]  # the connection was down at t2's close
    assert o4.verdict({"A": sym([], [t2], [])}, True, gap).startswith("INCONCLUSIVE_CONNECTION")
    assert o4.verdict({"A": sym([(t1, "Z1")], [t1, t2], [t1])}, True, gap).startswith("OBSERVED:")
    assert o4.verdict({"A": sym([], [], [])}, True).startswith("INCONCLUSIVE:")
    assert o4.verdict({"A": sym([], None, [])}, False).startswith("UNKNOWN")


def test_disconnect_windows_come_from_the_connection_events(o4: ModuleType) -> None:
    events = [
        {"event": "connected", "at": 1},
        {"event": "disconnected", "at": 10},
        {"event": "backoff", "at": 11},
        {"event": "connected", "at": 20},
        {"event": "aborted_by_user", "at": 50},
    ]
    assert o4.disconnect_windows(events, 60) == [(10, 20), (50, 60)]
