"""`ReferenceLiveStrategy` (MVP-3): live/backtest parity on identical closed 1m bars, causality,
warm-up, WATCH handling and REFERENCE labelling. Synthetic bars only (no lake, no network)."""

from __future__ import annotations

import ast
import dataclasses
import math
import random
from pathlib import Path
from typing import Any

import pytest

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.research.source import StrategySource
from tsclaude.sim.costs import CostModel
from tsclaude.sim.orders import EntryRequest
from tsclaude.strategies import BracketReference, BracketReferenceParams, PlanningCosts
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

T0 = 1_709_251_200_000  # 2024-03-01T00:00:00Z
M = 60_000
#: small windows so a two-day synthetic series yields CONFIRMED and WATCH proposals
PARAMS = BracketReferenceParams(
    atr_n=5, rank_m=10, rank_max=0.6, range_n=6, vol_n=6, vol_z_min=0.0, ema_span=5, rv_n=5,
    max_cost_r=0.5,
)  # fmt: skip
KEYS = [(Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")]
COSTS = {k: PlanningCosts.from_cost_model(CostModel(), *k) for k in KEYS}
TRADE_FROM = T0 + 600 * M
GAP = range(1500, 1504)  # minutes missing for spot BTC (a history break)


def minute_bars(
    market: Market, symbol: str, n: int, seed: int, skip: range = range(0)
) -> list[Bar]:
    rng = random.Random(seed)
    out: list[Bar] = []
    p = 100.0
    for i in range(n):
        regime = (i // 360) % 2
        sigma = 0.0006 if regime == 0 else 0.0035
        o = p
        c = o * math.exp(rng.gauss(0.0004 if regime else 0.0, sigma))
        h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
        p = c
        v = rng.uniform(1, 3) * (4 if regime else 1)
        if i in skip:
            continue
        t = T0 + i * M
        out.append(
            Bar(
                market=market, symbol=symbol, interval=Interval.M1, open_time_ms=t,
                close_time_ms=t + M - 1, open=o, high=h, low=lo, close=c, volume=v,
                quote_volume=v * c, trades=1, taker_buy_volume=v / 2,
                taker_buy_quote_volume=v * c / 2, source=BarSource.WS_LIVE, ingested_at_ms=t + M,
            )
        )  # fmt: skip
    return out


def series(n: int = 2880) -> list[Bar]:
    bars = minute_bars(*KEYS[0], n, 1, GAP) + minute_bars(*KEYS[1], n, 2)
    return sorted(bars, key=lambda b: (b.open_time_ms, str(b.market), b.symbol))


def backtest(bars: list[Bar]) -> tuple[StrategySource, list[EntryRequest]]:
    src = StrategySource(
        BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM, flat_from_ms=2**62
    )
    requests: list[EntryRequest] = []
    for b in bars:
        requests.extend(r for r in src.on_bar(b, None) if isinstance(r, EntryRequest))  # type: ignore[arg-type]
    return src, requests


def live(
    bars: list[Bar], *, warm_until: int = TRADE_FROM
) -> tuple[ReferenceLiveStrategy, list[Any]]:
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    adapter.warm_up([b for b in bars if b.close_time_ms < warm_until])
    intents: list[Any] = []
    for b in bars:
        intents.extend(adapter.on_bar(b, None))  # type: ignore[arg-type]
    return adapter, intents


def _req(r: EntryRequest) -> dict[str, Any]:
    d = dataclasses.asdict(r)
    for k in ("request_id", "signal_id", "books"):
        d.pop(k, None)
    return d


# ---------------------------------------------------------------------- R1 parity
def test_live_intents_equal_backtest_requests_on_identical_bars() -> None:
    bars = series()
    src, requests = backtest(bars)
    adapter, intents = live(bars)
    confirmed = [r for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    assert len(confirmed) >= 5  # not vacuous
    assert len(intents) == len(confirmed) == len(requests)
    for rec, intent, req in zip(confirmed, intents, requests, strict=True):
        assert intent.decision_time_ms == rec.decision_ms
        assert intent.stage is SignalStatus.CONFIRMED
        assert (intent.market, intent.symbol, intent.side) == (
            rec.market,
            rec.symbol,
            rec.proposal.side,
        )
        assert intent.entry_rule.kind is rec.proposal.entry_kind
        assert intent.entry_rule.price == rec.proposal.entry_price
        assert intent.stop_price == rec.proposal.stop_price
        assert intent.take_profit_price == rec.proposal.take_profit_price
        assert intent.timeframe is Interval.M15
        # the paper engine's request from the intent equals the backtest request field by field
        assert _req(EntryRequest.from_signal(intent)) == _req(req)
    watch = sum(1 for r in src.records if r.proposal.stage is SignalStatus.WATCH)
    assert watch > 0
    assert adapter.stats.watch_proposals == watch
    assert adapter.history_breaks == src.breaks > 0  # the gap broke the history the same way


def test_warm_up_overlapping_the_live_feed_changes_nothing() -> None:
    """Warm-up from the bar store and the queued live bars overlap by an hour: already-fed
    minutes are skipped, never aggregated twice, and the intents are unchanged."""
    bars = series()
    _, reference = live(bars)
    adapter, intents = live(bars, warm_until=TRADE_FROM + 60 * M)
    assert adapter.stats.skipped_not_newer > 0
    assert [i.model_dump() for i in intents] == [i.model_dump() for i in reference]


# ---------------------------------------------------------------------- R2 causality
def test_intents_up_to_t_do_not_depend_on_later_bars() -> None:
    bars = series()
    cut = T0 + 2000 * M
    _, full = live(bars)
    rng = random.Random(9)

    def scaled(b: Bar) -> Bar:  # a different, still valid future (all prices x f, volume x g)
        f, g = rng.uniform(0.5, 2.0), rng.uniform(0.1, 10.0)
        return b.model_copy(
            update={"open": b.open * f, "high": b.high * f, "low": b.low * f,
                    "close": b.close * f, "volume": b.volume * g}
        )  # fmt: skip

    altered = [b if b.close_time_ms <= cut else scaled(b) for b in bars]
    _, changed = live(altered)
    _, prefix = live([b for b in bars if b.close_time_ms <= cut])
    early = [i.model_dump() for i in full if i.decision_time_ms <= cut]
    assert early
    assert [i.model_dump() for i in changed if i.decision_time_ms <= cut] == early
    assert [i.model_dump() for i in prefix] == early


def test_every_intent_is_decided_at_the_close_of_the_bar_that_completed_it() -> None:
    bars = series()
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    adapter.warm_up([b for b in bars if b.close_time_ms < TRADE_FROM])
    for b in bars:
        for intent in adapter.on_bar(b, None):  # type: ignore[arg-type]
            assert intent.decision_time_ms == b.close_time_ms
            assert (b.close_time_ms + 1) % Interval.M15.ms == 0  # a 15m boundary
            assert intent.created_at_ms == intent.decision_time_ms


# ---------------------------------------------------------------------- R3 warm-up, R4 WATCH
def test_warm_up_and_bars_before_the_start_never_produce_intents() -> None:
    bars = series()
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    assert adapter.warm_up(bars) > 0  # only bars closing before the start are used
    assert adapter.stats.confirmed_intents == 0
    late = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=T0 + 2880 * M)
    produced = [i for b in bars for i in late.on_bar(b, None)]  # type: ignore[arg-type]
    assert produced == []  # everything before the start only builds history


def test_watch_proposals_never_become_intents() -> None:
    bars = series()
    adapter, intents = live(bars)
    assert adapter.stats.watch_proposals > 0
    assert all(i.stage is SignalStatus.CONFIRMED for i in intents)


# ---------------------------------------------------------------------- R6 labels, R7 fail closed
def test_reference_labels_and_deterministic_ids() -> None:
    bars = series()
    _, a = live(bars)
    _, b = live(bars)
    assert [i.signal_id for i in a] == [i.signal_id for i in b]
    assert len({i.signal_id for i in a}) == len(a)
    for intent in a:
        assert intent.evidence_grade == "X"
        assert intent.prereg_id is None
        assert intent.market_conditions["label"] == "REFERENCE"
        assert intent.market_conditions["validation"] == "NOT_VALIDATED"
        assert "NOT VALIDATED" in intent.rationale
        assert intent.strategy_id == "bracket_reference"


def test_time_exits_and_non_default_runtime_params_are_refused() -> None:
    with pytest.raises(ValueError, match="time exits"):
        ReferenceLiveStrategy(
            BracketReference(dataclasses.replace(PARAMS, time_exit_bars=4)), COSTS, trade_from_ms=0
        )
    adapter = ReferenceLiveStrategy.create(CostModel(), KEYS, trade_from_ms=TRADE_FROM)
    assert adapter.strategy.params == BracketReferenceParams()  # the runtime path: defaults only


def test_unknown_instruments_and_non_1m_bars_are_ignored() -> None:
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    eth = minute_bars(Market.SPOT, "ETHUSDT", 30, 5)
    assert all(adapter.on_bar(b, None) == [] for b in eth)  # type: ignore[arg-type]
    assert adapter.stats.bars_seen == 30


def test_the_adapter_has_no_network_or_order_path() -> None:
    """Simulation only: the module imports nothing that can reach an exchange."""
    path = (
        Path(__file__).resolve().parents[3] / "src" / "tsclaude" / "strategies" / "live_adapter.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    forbidden = ("httpx", "websockets", "requests", "aiohttp", "tsclaude.marketdata.binance")
    assert not [m for m in imported if m.startswith(forbidden)]


def test_parity_holds_when_the_session_starts_mid_bucket() -> None:
    """Audit N4: a real start rarely falls on a 15m boundary; warm-up and live bars then share
    the bucket in progress."""
    bars = series()
    trade_from = TRADE_FROM + 7 * M
    src = StrategySource(
        BracketReference(PARAMS), COSTS, trade_from_ms=trade_from, flat_from_ms=2**62
    )
    requests = [r for b in bars for r in src.on_bar(b, None) if isinstance(r, EntryRequest)]  # type: ignore[arg-type]
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=trade_from)
    adapter.warm_up([b for b in bars if b.close_time_ms < trade_from + 30 * M])  # overlaps live
    intents = [i for b in bars for i in adapter.on_bar(b, None)]  # type: ignore[arg-type]
    assert intents
    assert [_req(EntryRequest.from_signal(i)) for i in intents] == [_req(r) for r in requests]


def test_warmup_progress_reports_contiguous_bars_and_readiness() -> None:
    bars = series()
    adapter, _ = live(bars)
    progress = adapter.warmup_progress()
    assert set(progress) == {"spot:BTCUSDT", "usdm:BTCUSDT"}
    usdm = progress["usdm:BTCUSDT"]
    assert usdm["ready"] is True
    assert usdm["strategy_bars"] >= usdm["needed"] == adapter.strategy.warmup_bars
    # spot lost minutes 1500-1503: its history restarted after the gap
    assert progress["spot:BTCUSDT"]["strategy_bars"] < usdm["strategy_bars"]
    assert "7.8 days" in adapter.describe()["note"]


# ---------------------------------------------------------------------- option 1: 9-day history
#: minutes of startup history (configs/data_quality.yaml `backfill.lookback_days`)
NINE_DAYS = 9 * 1440


def _nine_day_series(live_minutes: int = 1440, skip: range = range(0)) -> tuple[list[Bar], int]:
    """9 days of closed history before a mid-bucket start, then `live_minutes` (synthetic)."""
    start = T0 + (NINE_DAYS + 7) * M
    n = NINE_DAYS + 7 + live_minutes
    bars = minute_bars(*KEYS[0], n, 3, skip) + minute_bars(*KEYS[1], n, 4)
    return sorted(bars, key=lambda b: (b.open_time_ms, str(b.market), b.symbol)), start


def _first_eligible(start: int) -> int:
    """Close of the 15m bucket in progress at the start: the first decision the source may take."""
    interval = Interval.M15.ms
    return start - start % interval + interval - 1


def test_nine_days_of_history_make_the_default_strategy_ready_for_its_first_decision() -> None:
    """Option 1: with the default params (741 x 15m warm-up, ~7.8 days) 9 days of closed history
    leave every instrument ready at the start, the first eligible decision is evaluated, and the
    live intents equal the backtest's on the same bars. Synthetic bars: the proposal counts are
    pipeline checks, not strategy results."""
    bars, start = _nine_day_series()
    adapter = ReferenceLiveStrategy(BracketReference(), COSTS, trade_from_ms=start)
    history = [b for b in bars if b.close_time_ms < start]
    assert adapter.warm_up(history) == len(history) == 2 * (NINE_DAYS + 7)
    progress = adapter.warmup_progress()
    assert all(p["ready"] and p["needed"] == 741 for p in progress.values()), progress
    assert adapter.stats.confirmed_intents == adapter.stats.watch_proposals == 0
    assert adapter._source.records == []  # warm-up never decides

    intents = [i for b in bars if b.close_time_ms >= start for i in adapter.on_bar(b, None)]  # type: ignore[arg-type]
    src = StrategySource(BracketReference(), COSTS, trade_from_ms=start, flat_from_ms=2**62)
    requests = [r for b in bars for r in src.on_bar(b, None) if isinstance(r, EntryRequest)]  # type: ignore[arg-type]
    for key in KEYS:
        assert adapter._source.decision_bars[key][0] == _first_eligible(start)
    assert adapter._source.decision_bars == src.decision_bars
    assert [(r.decision_ms, r.market, r.symbol, r.proposal) for r in adapter._source.records] == [
        (r.decision_ms, r.market, r.symbol, r.proposal) for r in src.records
    ]
    assert intents  # not vacuous
    assert [_req(EntryRequest.from_signal(i)) for i in intents] == [_req(r) for r in requests]
    assert adapter.stats.watch_proposals == sum(
        1 for r in src.records if r.proposal.stage is SignalStatus.WATCH
    )
    assert adapter.history_breaks == src.breaks == 0


def test_a_missing_minute_in_the_startup_history_resets_readiness() -> None:
    """A minute missing inside the last ~7.8 days of history leaves that instrument short of its
    warm-up at the start: no decision at the first eligible close. A minute missing before the
    last ~741 strategy bars does not matter (the runtime does not even read it)."""
    bars, start = _nine_day_series(live_minutes=60, skip=range(NINE_DAYS - 600, NINE_DAYS - 599))
    adapter = ReferenceLiveStrategy(BracketReference(), COSTS, trade_from_ms=start)
    adapter.warm_up([b for b in bars if b.close_time_ms < start])
    progress = adapter.warmup_progress()
    assert progress["usdm:BTCUSDT"]["ready"] is True
    spot = progress["spot:BTCUSDT"]
    assert spot["ready"] is False
    assert spot["strategy_bars"] < 50  # restarted ~10 h before the start
    for b in bars:
        if b.close_time_ms >= start:
            adapter.on_bar(b, None)  # type: ignore[arg-type]
    assert KEYS[0] not in adapter._source.decision_bars
    assert adapter._source.decision_bars[KEYS[1]][0] == _first_eligible(start)

    bars, start = _nine_day_series(live_minutes=60, skip=range(300, 301))  # day 0, ~8.8 days back
    adapter = ReferenceLiveStrategy(BracketReference(), COSTS, trade_from_ms=start)
    adapter.warm_up([b for b in bars if b.close_time_ms < start])
    assert adapter.history_breaks == 1
    assert all(p["ready"] for p in adapter.warmup_progress().values())
