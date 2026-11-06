"""M3-PB.3: the paper consumer drains what is already queued before it polls (fake market data,
synthetic bars, no network).

Before the fix `PaperRuntime._run` offered one update per poll. With a backlog (minutes queued while
paper waits for the market-data startup backfill), the first queued minute is already past its
settle deadline at that poll, so `LiveClock.release` released it with only its first bar and the
next bar of that minute became `late` and was never processed. The runtime now offers the awaited
update and every update already queued (`get_nowait`, at most `drain_max` per iteration) before it
snapshots, anchors and polls; when the bound is reached it defers that poll (an incomplete minute is
never released because of the bound).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from m1t2_fakes import FakeSubscription, make_bar
from test_mvp2_paper import KEYS, POLL, T0, GatedFactory, Rig, paper_config, run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "sim"))
from paper_helpers import record

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import Side, SignalRecord
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.runtime.paper_runtime import PaperSettings, PaperState
from tsclaude.sim.costs import Direction, Liquidity
from tsclaude.sim.engine import SimView
from tsclaude.sim.fills import BarPrices, resolve_exit
from tsclaude.sim.money import q8, to_decimal
from tsclaude.sim.paper import Intent, PaperTradingService, replay

M = MS_PER_MINUTE
SPOT = (Market.SPOT, "BTCUSDT")
USDM = (Market.USDM, "BTCUSDT")


def _settings(**kw: Any) -> PaperSettings:
    """The rig's settings (D-21 retry after one poll), plus overrides."""
    base: dict[str, Any] = {
        "enabled": True,
        "poll_s": POLL,
        "resync_retry_s": POLL,
        "resync_retry_max_s": POLL,
    }
    base.update(kw)
    return PaperSettings(**base)


async def _until(cond: Callable[[], bool], what: str, polls: int = 500) -> None:
    for _ in range(polls):
        if cond():
            return
        await asyncio.sleep(POLL)
    raise AssertionError(f"timed out waiting for {what}")


def _service(rig: Rig) -> PaperTradingService:
    service = rig.runtime.service
    assert service is not None
    return service


def _stats(rig: Rig) -> dict[str, int]:
    s = _service(rig).clock.stats
    return {
        "offered": s.offered,
        "released": s.released,
        "late": s.late,
        "incomplete_minutes": s.incomplete_minutes,
    }


async def _all_decided(rig: Rig, n: int) -> None:
    """Every offered bar is either released or late, and the consumer has polled since."""

    def done() -> bool:
        service = rig.runtime.service
        if service is None:
            return False
        s = service.clock.stats
        return s.offered == n and s.released + s.late == n

    await _until(done, f"{n} bars offered and decided")
    await asyncio.sleep(POLL * 3)


def _journal_keys(rig: Rig) -> list[tuple[int, str, str]]:
    out = []
    for batch in rig.journal():
        head = batch[0].payload
        if head["kind"] == "bar":
            bar = head["bar"]
            out.append((bar["open_time_ms"], bar["market"], bar["symbol"]))
    return out


def _expected_keys(minutes: Sequence[int]) -> list[tuple[int, str, str]]:
    """Clock order: minute, then (market, symbol)."""
    return sorted((T0 + i * M, str(m), s) for i in minutes for m, s in KEYS)


def _push(rig: Rig, i: int, bars: dict[tuple[Market, str], Bar], order: Sequence[Any]) -> None:
    rig.clock.set(T0 + (i + 1) * M + 1_500)
    for key in order:
        rig.md.push_bar(bars[key])


# ---------------------------------------------------------------------- A: the startup backlog
def test_a_backlog_queued_during_the_startup_wait_is_released_whole(tmp_path: Path) -> None:
    """Three minutes (two instruments each) queue while paper waits for the market-data backfill;
    when the gate opens every bar is released in clock order, none late, no minute incomplete."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        await rig.supervisor.start("S1")
        for i in range(3):  # queued, not processed: paper is still waiting
            await rig.minute(i)
        assert rig.runtime.service is None
        gate.done.set()  # the first poll happens now, at minute 2's close + 1.5 s
        await _all_decided(rig, 6)
        late = [(b.open_time_ms, str(b.market), b.symbol) for b in _service(rig).clock.late]
        assert (_stats(rig), late) == (
            {"offered": 6, "released": 6, "late": 0, "incomplete_minutes": 0},
            [],
        )
        assert _journal_keys(rig) == _expected_keys(range(3))  # journal order unchanged
        assert rig.runtime.state is PaperState.RUNNING
        # one awaited update, the five queued behind it drained before the single poll
        consumer = rig.runtime.published()[0]["consumer"]
        assert consumer == {
            "drain_max": 1000,
            "drained_updates": 5,
            "largest_drain": 5,
            "deferrals": 0,
        }
        # D-21 unchanged: the anchor comes from this run's fresh round, after the backlog
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["observing_from_ms"] == T0 + 4 * M
        assert observation["resync"]["attempts"] == 1
        assert rig.md.resync_calls == 1
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- B: an exposed position
STOP = 99.0
TP = 103.0
FLAT = (100.0, 100.2, 99.8, 100.0)
CROSS = (100.0, 100.2, 98.5, 98.8)  # minute 4 spot: trades through the stop (not at the open)
GAPPED = (98.6, 98.9, 98.4, 98.7)  # minute 5 spot: opens below the stop


def _ohlc(key: tuple[Market, str], i: int, ohlc: tuple[float, float, float, float]) -> Bar:
    o, h, lo, c = ohlc
    return make_bar(*key, T0 + i * M).model_copy(
        update={"open": o, "high": h, "low": lo, "close": c}
    )


def _minute_bars(i: int, spot: tuple[float, float, float, float] = FLAT) -> dict[Any, Bar]:
    return {SPOT: _ohlc(SPOT, i, spot), USDM: _ohlc(USDM, i, FLAT)}


class OneEntry:
    """Live strategy: one long spot BTCUSDT signal with a stop at the close of `minute`."""

    def __init__(self, minute: int) -> None:
        self.at = T0 + minute * M
        base = record(1, 0, ref=100.0, stop=STOP, tp=TP)
        decision = self.at + M - 1
        self.signal: SignalRecord = base.model_copy(
            update={
                "decision_time_ms": decision,
                "created_at_ms": decision,
                "entry_rule": base.entry_rule.model_copy(
                    update={"valid_until_ms": decision + 5 * M}
                ),
            }
        )
        self.calls: list[tuple[int, str, str]] = []

    def on_bar(self, bar: Bar, view: SimView) -> list[Intent]:
        self.calls.append((bar.open_time_ms, str(bar.market), bar.symbol))
        if (bar.open_time_ms, bar.market, bar.symbol) == (self.at, *SPOT):
            return [self.signal]
        return []


def _fills(service: PaperTradingService) -> list[dict[str, Any]]:
    return [
        {"book": e["aggregate_id"], **e["payload"]}
        for e in service.session.sim.events
        if e["type"] == "sim_order_filled"
    ]


def test_b_stop_in_a_bar_queued_behind_its_minute_fills_in_that_minute(tmp_path: Path) -> None:
    """A position with a resting stop is open across a restart. While the restart waits for the
    market-data backfill three minutes queue; minute 4's spot bar (delivered second within its
    minute) trades through the stop, minute 5 opens below it. The stop must fill on minute 4 at the
    execution model's price, never on a later bar (before the fix minute 4's spot bar was late and
    the stop filled at minute 5's gapped open). The restart's minutes are processed unobserved
    (P-1): the resting stop still resolves on them."""

    async def scenario() -> None:
        gate = GatedFactory()
        gate.done.set()  # S1: nothing to backfill
        rig = Rig(tmp_path, factory=gate)
        strategy = OneEntry(minute=1)
        rig.runtime._strategy_factory = lambda: strategy
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        await rig.idle(T0 + 20_000)
        await _until(
            lambda: _service(rig).observing_from_ms is not None, "the S1 observation anchor"
        )
        assert _service(rig).observing_from_ms == T0 + M
        for i in range(4):
            await rig.idle(T0 + (i + 1) * M - 500)  # a fresh market view just before the close
            _push(rig, i, _minute_bars(i), (SPOT, USDM))
            await asyncio.sleep(POLL * 6)
        # minute 1 decides; release time bounds execution, so the entry fills at minute 3's open
        entries = [
            f for f in _fills(_service(rig)) if f["role"] == "entry" and "SIG" in f["order_id"]
        ]
        assert sorted((f["book"], f["bar_open_ms"]) for f in entries) == [
            ("baseline", T0 + 3 * M),
            ("shadow", T0 + 3 * M),
        ]
        exposed = {f["book"]: f for f in entries}
        books = _service(rig).session.sim.books
        assert all(books[name].positions_snapshot().get(SPOT) for name in exposed)
        await rig.supervisor.stop()
        assert _journal_keys(rig) == _expected_keys(range(4))

        gate.done = asyncio.Event()  # S2: the market-data backfill is still running
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.STARTING
        bars = {4: _minute_bars(4, CROSS), 5: _minute_bars(5, GAPPED), 6: _minute_bars(6)}
        for i in (4, 5, 6):  # spot second within each minute
            _push(rig, i, bars[i], (USDM, SPOT))
            await asyncio.sleep(POLL * 2)
        assert rig.runtime.service is None
        gate.done.set()  # first poll at minute 6's close + 1.5 s: minute 4 is past its deadline
        await _all_decided(rig, 6)
        service = _service(rig)

        # the stop price the execution model gives for minute 4's spot bar
        cross = bars[4][SPOT]
        prices = BarPrices.from_bar(cross)
        spec = next(s for s in service.config.instruments if s.key == SPOT)
        expected: dict[str, tuple[int, str, Decimal]] = {}
        for name, entry in exposed.items():
            fill = resolve_exit(
                prices.path(),
                Side.LONG,
                to_decimal(STOP),
                to_decimal(TP),
                spec.tick_size,
                held_at_open=True,
                market_exit_due=None,
                tp_liquidity=Liquidity.TAKER,  # unused: the take-profit is not reached
            )
            assert fill is not None
            qty = Decimal(entry["qty"])
            execution = service.config.cost_model.price(
                market=Market.SPOT,
                symbol="BTCUSDT",
                direction=Direction.SELL,
                liquidity=fill.liquidity,
                raw_price=fill.raw_price,
                tick_size=spec.tick_size,
                notional_hint=q8(qty * fill.raw_price),
                bar_quote_volume=to_decimal(cross.quote_volume),
            )
            expected[name] = (T0 + 4 * M, str(fill.reason), execution.price)
        exits = {
            f["book"]: (f["bar_open_ms"], f["role"], Decimal(f["price"]))
            for f in _fills(service)
            if f["role"] != "entry" and f["book"] in exposed
        }
        got = (_stats(rig), exits)
        assert got == (
            {"offered": 6, "released": 6, "late": 0, "incomplete_minutes": 0},
            expected,
        ), _fills(service)
        # no fill in any book uses a bar after minute 4 (the backlog's only crossing bar)
        assert all(f["bar_open_ms"] <= T0 + 4 * M for f in _fills(service))
        assert not any(service.session.sim.books[name].positions_snapshot() for name in exposed)
        snap = rig.runtime.published()[0]
        assert all(b["ledger_reconciled"] for b in snap["books"].values())
        # the restart's minutes were processed unobserved (P-1): only the resting stop acted
        flags = {
            (p["bar"]["open_time_ms"], p["bar"]["market"]): p["observed"]
            for p in (b[0].payload for b in rig.journal())
        }
        assert flags[(T0 + 4 * M, "spot")] is False
        assert all(t < T0 + 4 * M for t, _, _ in strategy.calls)
        assert _journal_keys(rig) == _expected_keys(range(7))
        # the whole journal (both runs) replays to the same ledgers
        assert replay(paper_config(), rig.journal()).ledger_hashes() == service.ledger_hashes()
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- the bound (drain_max)
def test_bound_defers_the_poll_and_never_releases_a_minute_incomplete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """drain_max=2 with twelve queued updates: three per iteration, so batch boundaries split
    minutes 1 and 4 (minute 1 is past its deadline). A bounded iteration neither snapshots, anchors
    nor polls; the poll follows only once the queue was seen empty."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        rig.runtime.settings = _settings(drain_max=2)
        await rig.supervisor.start("S1")
        for i in range(6):
            await rig.minute(i)
        gate.done.set()  # first poll at minute 5's close + 1.5 s: minutes 0-3 past the deadline
        await _all_decided(rig, 12)
        assert _stats(rig) == {"offered": 12, "released": 12, "late": 0, "incomplete_minutes": 0}
        assert _journal_keys(rig) == _expected_keys(range(6))
        # four bounded iterations (1 awaited + 2 drained each), then an empty get() and the poll.
        # Deterministic: the whole backlog was queued before the consumer's first get().
        consumer = rig.runtime.published()[0]["consumer"]
        assert consumer == {
            "drain_max": 2,
            "drained_updates": 8,
            "largest_drain": 2,
            "deferrals": 4,
        }
        assert len(_deferral_logs(caplog)) == 4  # one log line per deferral
        # the deferred anchor step still comes from one fresh round, at the first real poll
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["observing_from_ms"] == T0 + 7 * M
        assert observation["resync"]["attempts"] == 1
        assert rig.runtime.state is PaperState.RUNNING
        # Later minutes are processed normally. Minute 6's two bars are pushed in one event-loop
        # turn: if the consumer's get(timeout) expires in that turn, the drain then takes exactly
        # drain_max (2) and that iteration defers once more (the conservative exact-bound case),
        # so the deferral count after this point depends on scheduling (finding A-1). The release
        # outcome does not: both bars in their minute, none late, nothing incomplete.
        await rig.minute(6)
        assert _stats(rig) == {"offered": 14, "released": 14, "late": 0, "incomplete_minutes": 0}
        assert _journal_keys(rig) == _expected_keys(range(7))
        # every deferral, scheduled or not, is counted and logged exactly once; minute 6 adds at
        # most the one exact-bound deferral, and every deferral is a full drain of drain_max
        assert len(_deferral_logs(caplog)) == rig.runtime._drain_deferrals
        assert rig.runtime._drain_deferrals in (4, 5)
        assert all(r.drained == 2 for r in _deferral_logs(caplog))
        await rig.supervisor.close()

    with caplog.at_level(logging.INFO, logger="tsclaude.runtime.paper_runtime"):
        run(scenario())


def _deferral_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "paper poll deferred" in r.getMessage()]


def test_drain_max_setting_is_validated_and_loaded(tmp_path: Path) -> None:
    assert PaperSettings().drain_max == 1000
    for bad in (0, -1):
        with pytest.raises(ValueError, match="drain_max"):
            PaperSettings(drain_max=bad)
    path = tmp_path / "paper.yaml"
    path.write_text("enabled: true\ndrain_max: 7\n", encoding="utf-8")
    assert PaperSettings.load(path).drain_max == 7
    path.write_text("enabled: true\n", encoding="utf-8")
    assert PaperSettings.load(path).drain_max == 1000
    path.write_text("enabled: true\ndrain_max: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="drain_max"):
        PaperSettings.load(path)


# ---------------------------------------------------------------------- closed / failed streams
def test_subscription_closed_behind_the_backlog(tmp_path: Path) -> None:
    """Market data stops (running cleared, subscription closed) while the backlog is queued: the
    drain stops at the close sentinel (left queued), the queued bars are polled once, and the next
    `get()` reports the closure: SUBSCRIPTION_LOST as before."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        sub = rig.paper_sub()
        await rig.md.stop()  # not through the supervisor: paper has not even opened yet
        assert sub.closed
        gate.done.set()
        await _until(lambda: rig.runtime.state is PaperState.SUBSCRIPTION_LOST, "the closure")
        assert _stats(rig) == {"offered": 6, "released": 6, "late": 0, "incomplete_minutes": 0}
        assert _journal_keys(rig) == _expected_keys(range(3))
        assert sub.queue.empty()  # the sentinel was left for get(), which consumed it
        await rig.supervisor.close()

    run(scenario())


def test_subscription_closed_during_bounded_iterations_releases_nothing_incomplete(
    tmp_path: Path,
) -> None:
    """Same closure with drain_max=2: the bounded iterations never poll, and the closure halts
    paper before any poll, so nothing is released (fail closed) and no minute is split."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        rig.runtime.settings = _settings(drain_max=2)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.md.stop()
        gate.done.set()
        await _until(lambda: rig.runtime.state is PaperState.SUBSCRIPTION_LOST, "the closure")
        assert _stats(rig) == {"offered": 6, "released": 0, "late": 0, "incomplete_minutes": 0}
        assert rig.journal() == []
        assert rig.runtime.published()[0]["consumer"]["deferrals"] == 2
        await rig.supervisor.close()

    run(scenario())


def test_stream_failure_in_the_drain_stops_paper_before_the_poll(tmp_path: Path) -> None:
    """A stream failure queued behind a bar propagates from the drain (CONSUMER_DIED, as from
    `get()`); the iteration that met it never polls."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        sub = rig.paper_sub()
        sub.queue.put_nowait(RuntimeError("subscription broken"))
        gate.done.set()
        await _until(lambda: rig.runtime.state is PaperState.CONSUMER_DIED, "the failure")
        assert "subscription broken" in str(rig.runtime.reason)
        assert rig.journal() == []
        await rig.supervisor.close()

    run(scenario())


def test_overflow_is_checked_after_the_drain(tmp_path: Path) -> None:
    """A drop counted while the drain runs stops paper before the poll (STOPPED_OVERFLOW)."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        await rig.supervisor.start("S1")
        for i in range(2):
            await rig.minute(i)
        sub = rig.paper_sub()
        original = sub.get_nowait
        calls = 0

        def dropping() -> BarClosed | Any:
            nonlocal calls
            calls += 1
            if calls == 2:  # the queue overflowed while the consumer was draining
                sub.dropped = 1  # type: ignore[attr-defined]
            return original()

        sub.get_nowait = dropping  # type: ignore[method-assign]
        gate.done.set()
        await _until(lambda: rig.runtime.state is PaperState.STOPPED_OVERFLOW, "the overflow")
        assert rig.runtime.dropped_updates == 1
        assert rig.journal() == []
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- the fake mirrors the real
def test_fake_subscription_get_nowait_mirrors_the_real_subscription() -> None:
    async def scenario() -> None:
        from m1t2_fakes import FakeMarketData

        md = FakeMarketData.__new__(FakeMarketData)
        md.subscriptions = set()
        sub = FakeSubscription(md)
        assert sub.get_nowait() is None
        assert sub.closed is False
        items = [BarClosed(make_bar(*SPOT, T0 + i * M)) for i in range(3)]
        for item in items:
            sub.queue.put_nowait(item)
        assert sub.get_nowait() is items[0]
        assert await sub.get(timeout=1.0) is items[1]
        sub.close()
        assert sub.closed is True
        assert sub.get_nowait() is items[2]
        for _ in range(3):  # the close sentinel stays queued for get()
            assert sub.get_nowait() is None
        assert await sub.get(timeout=1.0) is None
        failing = FakeSubscription(md)
        failing.queue.put_nowait(RuntimeError("broken"))
        with pytest.raises(RuntimeError, match="broken"):
            failing.get_nowait()
        assert failing.get_nowait() is None  # consumed, like get()

    run(scenario())
