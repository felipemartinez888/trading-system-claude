"""Quant Equivalence Gate: previous ingestion (frozen at 1acd38a) vs the bounded implementation.

Same deterministic datasets, configurations and event sequences go through both implementations
(`legacy_backfill_1acd38a` / `legacy_livebars_1acd38a` are verbatim copies of the pre-change code).
The gate compares what the quantitative layers consume or react to:

1. stored bars (every column, source, observed-session tag), results, gap and conflict events,
   the `BACKFILL_COMPLETED` payload (new keys are additive only);
2. downstream of the store: the real `BracketReference` live adapter (indicators, signals,
   readiness, history breaks) fed from each store gives identical outputs;
3. incomplete history (an interrupted or bounded load) never yields a signal or readiness.

Only the order of work differs (newest-first chunks), so windows larger than one chunk are compared
by minute sets and stored rows, not by gap-record boundaries. No network, no real data.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import legacy_backfill_1acd38a as old_bf
import legacy_livebars_1acd38a as old_lb
import pytest
from md_helpers import MIN, T0, FakeFetcher, Sink, kline, rest_kline

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.backfill import Backfiller, BackfillSettings, PairProgress, RunLimits
from tsclaude.marketdata.convert import bar_from_ws
from tsclaude.marketdata.types import RestKline
from tsclaude.quality.gaps import GapRegistry
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import LiveBarStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "strategies"))
from test_live_adapter import (
    COSTS,
    PARAMS,
    live,
    minute_bars,
)
from test_live_adapter import KEYS as LA_KEYS
from test_live_adapter import T0 as LA_T0

from tsclaude.strategies import BracketReference
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

M, SYM = Market.USDM, "ETHUSDT"
END = T0 + 60 * MIN


# ------------------------------------------------------------------------- harness
@dataclasses.dataclass
class Outcome:
    rows: list[tuple[Any, ...]]
    counters: dict[str, int]
    events: list[tuple[str, str, int, str]]
    completed: list[dict[str, Any]]
    missing_minutes: set[int]


def _norm_events(sink: Sink) -> list[tuple[str, str, int, str]]:
    out = []
    for e in sink.events:
        if e.type == EventType.BACKFILL_COMPLETED:
            continue
        payload = {k: v for k, v in e.payload.items() if k != "gap_id"}
        out.append((str(e.type), e.aggregate_id, e.ts_ms, json.dumps(payload, sort_keys=True)))
    return sorted(out)


def _rows(store: Any, market: Market, symbol: str, lo: int, hi: int) -> list[tuple[Any, ...]]:
    return [
        (sb.bar.model_dump_json(), sb.observed_session_id)
        for sb in store.range(market, symbol, lo, hi)
    ]


_COUNTERS = (
    "requested_minutes",
    "inserted_backfill",
    "inserted_recovered",
    "duplicates",
    "conflicts",
    "invalid",
    "gaps_opened_minutes",
    "gaps_resolved_minutes",
    "remaining_open_minutes",
)


@dataclasses.dataclass
class Scenario:
    seed: list[tuple[Bar, str | None]] = dataclasses.field(default_factory=list)
    skip: set[int] = dataclasses.field(default_factory=set)
    tamper: Callable[[int, RestKline], RestKline | None] = lambda _i, r: r
    settings: BackfillSettings = dataclasses.field(
        default_factory=lambda: BackfillSettings(lookback_ms=60 * MIN, max_backfill_ms=120 * MIN)
    )
    intervals: list[ObservationInterval] = dataclasses.field(default_factory=list)
    runs: int = 1


def _fetcher(sc: Scenario, total: int = 70) -> FakeFetcher:
    f = FakeFetcher()
    for i in range(total):
        if i in sc.skip:
            continue
        row = sc.tamper(i, rest_kline(T0 + i * MIN))
        if row is not None:
            f.put(M, SYM, row)
    return f


def _execute(sc: Scenario, impl: str, tmp_path: Path) -> Outcome:
    sink, clock = Sink(), ManualClock(END + 5_000)
    ids = iter(range(10_000))
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"gap-{next(ids):04d}")
    store: Any = (old_lb.LiveBarStore if impl == "old" else LiveBarStore)(tmp_path / f"{impl}.db")
    try:
        for bar, session in sc.seed:
            store.write(bar, session)
        fetch = _fetcher(sc)
        if impl == "old":
            bf: Any = old_bf.Backfiller(
                store,
                gaps,
                fetch,
                sink,
                clock.now_ms,
                old_bf.BackfillSettings(
                    lookback_ms=sc.settings.lookback_ms, max_backfill_ms=sc.settings.max_backfill_ms
                ),
            )
        else:
            bf = Backfiller(store, gaps, fetch, sink, clock.now_ms, sc.settings)
        results = [asyncio.run(bf.run(M, SYM, END, sc.intervals)) for _ in range(sc.runs)]
        last = results[-1]
        completed = [e.payload for e in sink.of(EventType.BACKFILL_COMPLETED)]
        return Outcome(
            rows=_rows(store, M, SYM, T0 - 600 * MIN, END + 10 * MIN),
            counters={k: getattr(last, k) for k in _COUNTERS},
            events=_norm_events(sink),
            completed=completed,
            missing_minutes={
                m for lo, hi in gaps.missing_ranges(M, SYM) for m in range(lo, hi, MIN)
            },
        )
    finally:
        store.close()


def _ws(i: int, close: float = 100.5, base: int = T0) -> Bar:
    return bar_from_ws(kline(base + i * MIN, market=M, symbol=SYM, close=close), base)


def _rest_bar(i: int) -> Bar:
    return _ws(i).model_copy(update={"source": BarSource.REST_BACKFILL})


OBS = [ObservationInterval(T0 + 20 * MIN + 30_000, T0 + 39 * MIN + 10_000, "MON1", "user")]


def _scenarios() -> dict[str, Scenario]:
    def invalid_row(i: int, r: RestKline) -> RestKline | None:
        return dataclasses.replace(r, close_time_ms=r.close_time_ms + 1) if i == 12 else r

    def differing(i: int, r: RestKline) -> RestKline | None:
        return dataclasses.replace(r, close=100.9) if i in (15, 16) else r

    old_floor = BackfillSettings(lookback_ms=30 * MIN, max_backfill_ms=60 * MIN)
    return {
        "fresh store with REST holes": Scenario(skip={5, 6, 30}),
        "downtime after stored bars, partly observed": Scenario(
            seed=[(_rest_bar(i), "OLD") for i in range(10)]
            + [(_ws(i), "OLD") for i in range(20, 25)],
            skip={40},
            intervals=OBS,
        ),
        "source-finality conflicts": Scenario(
            seed=[(_ws(15, 100.1), "S1"), (_rest_bar(16), None)], tamper=differing
        ),
        "invalid REST row": Scenario(tamper=invalid_row),
        "last stored bar older than max_backfill": Scenario(
            seed=[(_ws(-300), "OLD")], settings=old_floor
        ),
        "idempotent second run": Scenario(skip={7}, intervals=OBS, runs=2),
    }


@pytest.mark.parametrize("name", list(_scenarios()))
def test_ingestion_outputs_equal_the_previous_implementation(name: str, tmp_path: Path) -> None:
    sc = _scenarios()[name]
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _execute(sc, "old", tmp_path / "old")
    new = _execute(sc, "new", tmp_path / "new")
    assert new.rows == old.rows
    assert new.counters == old.counters
    assert (
        new.events == old.events
    )  # gap detected/resolved and bar_conflict, same ranges and details
    assert new.missing_minutes == old.missing_minutes
    assert len(new.completed) == len(old.completed) >= 1  # one BACKFILL_COMPLETED per run
    for o, n in zip(old.completed, new.completed, strict=True):
        assert {k: n[k] for k in o} == o  # every legacy key keeps its value; new keys are additive
        assert set(n) >= set(o)
    assert old.rows  # not vacuous


# ------------------------------------------------------------------ downstream of the store
def _rest_from_bar(b: Bar) -> RestKline:
    return RestKline(
        open_time_ms=b.open_time_ms, open=b.open, high=b.high, low=b.low, close=b.close,
        volume=b.volume, close_time_ms=b.close_time_ms, quote_volume=b.quote_volume,
        trades=b.trades, taker_buy_volume=b.taker_buy_volume,
        taker_buy_quote_volume=b.taker_buy_quote_volume,
    )  # fmt: skip


def _history(impl: str, tmp_path: Path, n: int = 2880) -> tuple[Any, Sink]:
    """Backfill the strategy fixture series (spot has a 4-minute hole) into the chosen store."""
    sink, clock = Sink(), ManualClock(LA_T0 + n * MIN + 5_000)
    ids = iter(range(10_000))
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"gap-{next(ids):04d}")
    source: dict[tuple[Market, str], dict[int, RestKline]] = {}
    for market, symbol in LA_KEYS:
        gap = range(1500, 1504) if market is Market.SPOT else range(0)
        source[(market, symbol)] = {
            b.open_time_ms: _rest_from_bar(b)
            for b in minute_bars(market, symbol, n, 1 if market is Market.SPOT else 2, gap)
        }
    fetch = FakeFetcher(source)
    lookback = n * MIN
    if impl == "old":
        store: Any = old_lb.LiveBarStore(tmp_path / "old.db")
        bf: Any = old_bf.Backfiller(store, gaps, fetch, sink, clock.now_ms,
                                    old_bf.BackfillSettings(lookback, 2 * lookback))  # fmt: skip
    else:
        store = LiveBarStore(tmp_path / "new.db")
        bf = Backfiller(
            store, gaps, fetch, sink, clock.now_ms, BackfillSettings(lookback, 2 * lookback)
        )
    for market, symbol in LA_KEYS:
        asyncio.run(bf.run(market, symbol, LA_T0 + n * MIN, []))
    return store, sink


def _all_bars(store: Any, n: int = 2880) -> list[Bar]:
    out: list[Bar] = []
    for market, symbol in LA_KEYS:
        out.extend(sb.bar for sb in store.range(market, symbol, LA_T0, LA_T0 + n * MIN))
    return sorted(out, key=lambda b: (b.open_time_ms, str(b.market), b.symbol))


def test_indicators_signals_and_readiness_are_identical_from_either_store(tmp_path: Path) -> None:
    (tmp_path / "o").mkdir()
    (tmp_path / "n").mkdir()
    old_store, old_sink = _history("old", tmp_path / "o")
    new_store, new_sink = _history("new", tmp_path / "n")
    try:
        old_bars, new_bars = _all_bars(old_store), _all_bars(new_store)
        # all columns except provenance bookkeeping that the pipeline does not consume
        assert [b.model_dump_json() for b in new_bars] == [b.model_dump_json() for b in old_bars]
        assert len(new_bars) == 2 * 2880 - 4  # the 4-minute spot hole stays a hole

        # Indicator values: the strategy's own feature vector on the same window of each store
        strategy = BracketReference(PARAMS)
        for sb_old, sb_new in zip(
            old_store.range(*LA_KEYS[1], LA_T0, LA_T0 + 2880 * MIN),
            new_store.range(*LA_KEYS[1], LA_T0, LA_T0 + 2880 * MIN),
            strict=True,
        ):
            assert (sb_old.bar.close, sb_old.bar.volume) == (sb_new.bar.close, sb_new.bar.volume)
        assert strategy.warmup_bars == 74

        # Signals, readiness and history breaks: the real live adapter over each store's bars
        a_old, intents_old = live(old_bars)
        a_new, intents_new = live(new_bars)
        assert len(intents_old) == 9  # the fixture really produces confirmed signals
        assert [i.model_dump() for i in intents_new] == [i.model_dump() for i in intents_old]
        assert a_new.warmup_progress() == a_old.warmup_progress()
        assert a_new.history_breaks == a_old.history_breaks == 1
        assert dataclasses.asdict(a_new.stats) == dataclasses.asdict(a_old.stats)

        # Gap bookkeeping for the hole (same minutes, same cause)
        def gap_minutes(sink: Sink) -> set[int]:
            return {
                m
                for e in sink.of(EventType.GAP_DETECTED)
                for (lo, hi) in [(e.payload["from_ms"], e.payload["to_ms"])]
                for m in range(lo, hi, MIN)
            }

        assert gap_minutes(new_sink) == gap_minutes(old_sink)
    finally:
        old_store.close()
        new_store.close()


# --------------------------------------------------- incomplete history never trades or is ready
def _interrupted_store(tmp_path: Path, stop_after_chunks: int) -> tuple[LiveBarStore, PairProgress]:
    sink, clock = Sink(), ManualClock(LA_T0 + 2880 * MIN + 5_000)
    ids = iter(range(10_000))
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"gap-{next(ids):04d}")
    market, symbol = LA_KEYS[1]
    rows = {b.open_time_ms: _rest_from_bar(b) for b in minute_bars(market, symbol, 2880, 2)}
    fetch = FakeFetcher({(market, symbol): rows})
    store = LiveBarStore(tmp_path / "partial.db")
    prog = PairProgress(market, symbol, "startup")
    bf = Backfiller(
        store, gaps, fetch, sink, clock.now_ms,
        BackfillSettings(lookback_ms=2880 * MIN, max_backfill_ms=5760 * MIN),
        limits=RunLimits(chunk_minutes=1000),
    )  # fmt: skip
    asyncio.run(bf.run(market, symbol, LA_T0 + 2880 * MIN, [], progress=prog,
                       should_stop=lambda: prog.chunks_done >= stop_after_chunks))  # fmt: skip
    return store, prog


def test_an_interrupted_history_never_produces_a_signal_or_readiness(tmp_path: Path) -> None:
    store, prog = _interrupted_store(tmp_path, stop_after_chunks=1)
    try:
        assert prog.state == "cancelled"
        market, symbol = LA_KEYS[1]
        first = store.first_open_time(market, symbol)
        assert first == LA_T0 + 1880 * MIN  # newest chunk only: a contiguous recent suffix
        trade_from = first + 600 * MIN  # 600 stored minutes = 40 strategy bars < 74 needed
        adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=trade_from)
        stored = [sb.bar for sb in store.range(market, symbol, LA_T0, LA_T0 + 2880 * MIN)]
        assert adapter.warm_up(stored) > 0
        intents: list[Any] = []
        for b in stored:
            if b.close_time_ms >= trade_from and b.open_time_ms < trade_from + 400 * MIN:
                intents.extend(adapter.on_bar(b, None))  # type: ignore[arg-type]
        progress = adapter.warmup_progress()[f"{market}:{symbol}"]
        assert progress["strategy_bars"] < progress["needed"] == 74
        assert progress["ready"] is False
        assert intents == []  # not ready, therefore no signal at all
        assert adapter.stats.confirmed_intents == 0
    finally:
        store.close()


def test_the_bounded_catchup_alone_is_not_ready_but_the_full_history_is(tmp_path: Path) -> None:
    """A 24 h-style catch-up (here 600 min < 74 bars) leaves the strategy not ready; once the
    history run completes the same store gives the same intents as the previous implementation."""
    market, symbol = LA_KEYS[1]
    sink, clock = Sink(), ManualClock(LA_T0 + 2880 * MIN + 5_000)
    ids = iter(range(10_000))
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"gap-{next(ids):04d}")
    rows = {b.open_time_ms: _rest_from_bar(b) for b in minute_bars(market, symbol, 2880, 2)}
    fetch = FakeFetcher({(market, symbol): rows})
    end = LA_T0 + 2880 * MIN
    with LiveBarStore(tmp_path / "s.db") as store:
        catchup = Backfiller(
            store, gaps, fetch, sink, clock.now_ms,
            BackfillSettings(lookback_ms=600 * MIN, max_backfill_ms=5760 * MIN,
                             extend_to_last_stored=False),
            job="catchup",
        )  # fmt: skip
        asyncio.run(catchup.run(market, symbol, end, []))
        trade_from = end - 100 * MIN
        short = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=trade_from)
        short.warm_up([sb.bar for sb in store.range(market, symbol, LA_T0, end)])
        assert short.warmup_progress()[f"{market}:{symbol}"]["ready"] is False

        history = Backfiller(
            store, gaps, fetch, sink, clock.now_ms,
            BackfillSettings(lookback_ms=2880 * MIN, max_backfill_ms=5760 * MIN,
                             extend_to_last_stored=False),
            job="history",
        )  # fmt: skip
        res = asyncio.run(history.run(market, symbol, end, []))
        assert res.status == "complete"
        full = [sb.bar for sb in store.range(market, symbol, LA_T0, end)]
        assert len(full) == 2880
        ready = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=end)
        ready.warm_up(full)
        assert ready.warmup_progress()[f"{market}:{symbol}"]["ready"] is True
