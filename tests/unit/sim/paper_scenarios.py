"""Seeded random paper scenarios and the property checks shared by the seeded unit tests (run
offline) and the hypothesis properties (`tests/property/sim/test_paper_properties.py`, CI)."""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import Decimal as D

from paper_helpers import BTC, ETH, ScriptedStrategy, config, drive, new_service, record, sid

from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine, RiskStatus
from tsclaude.risk.market import MarketView, SymbolMarket
from tsclaude.sim.clock import HistoricalClock, order_key
from tsclaude.sim.costs import CostModel
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.live import LiveClock
from tsclaude.sim.paper import Confirm, Intent, Invalidate, PaperTradingService, replay
from tsclaude.sim.testing import bar, minute

SYMBOLS = (BTC, ETH)


def random_bars(
    rng: random.Random, n: int, gap_prob: float = 0.0
) -> dict[tuple[Market, str], list[Bar]]:
    out: dict[tuple[Market, str], list[Bar]] = {}
    for key in SYMBOLS:
        price = 10_000  # ticks of 0.1
        seq: list[Bar] = []
        for i in range(n):
            o = price + rng.randint(-3, 3)
            c = max(100, o + rng.randint(-25, 25))
            h = max(o, c) + rng.randint(0, 15)
            lo = min(o, c) - rng.randint(0, 15)
            price = c
            b = bar(i, D(o) / 10, D(h) / 10, D(lo) / 10, D(c) / 10, symbol=key[1], market=key[0])
            seq.append(b.model_copy(update={"source": BarSource.WS_LIVE}))
        out[key] = seq
    return out


def random_script(
    rng: random.Random, bars: dict[tuple[Market, str], list[Bar]], n: int
) -> ScriptedStrategy:
    script: dict[tuple[int, str], list[Intent]] = {}
    count = 0
    watches: list[tuple[str, int, str]] = []
    for i in range(1, n - 3):
        for key in SYMBOLS:
            r = rng.random()
            close = float(bars[key][i].close)
            if r < 0.18:
                count += 1
                sign = 1
                risk = rng.choice([3.0, 6.0, 12.0])
                kind = rng.choice(list(EntryRuleKind))
                price = None
                anchor = close
                if kind is EntryRuleKind.LIMIT:
                    price = round(close - rng.randint(0, 20) / 10, 1)
                    anchor = price
                elif kind is EntryRuleKind.STOP:
                    price = round(close + rng.randint(1, 20) / 10, 1)
                    anchor = price
                stage = SignalStatus.WATCH if rng.random() < 0.25 else SignalStatus.CONFIRMED
                rec = record(
                    count,
                    i,
                    symbol=key[1],
                    side=Side.LONG,
                    stage=stage,
                    ref=close,
                    stop=round(anchor - sign * risk, 1),
                    tp=round(anchor + sign * risk * rng.choice([1.0, 2.5, 4.0]), 1),
                    kind=kind,
                    price=price,
                    valid_bars=rng.randint(2, 7),
                )
                script.setdefault((minute(i), key[1]), []).append(rec)
                if stage is SignalStatus.WATCH:
                    watches.append((sid(count), i, key[1]))
            elif r < 0.22 and count:
                target = sid(rng.randint(1, count))
                script.setdefault((minute(i), key[1]), []).append(Invalidate(target))
        for wid, at, symbol in list(watches):
            if i > at and rng.random() < 0.3:
                script.setdefault((minute(i), symbol), []).append(Confirm(wid))
                watches.remove((wid, at, symbol))
    return ScriptedStrategy(script)


def run_random_session(seed: int, n: int = 40) -> tuple[PaperTradingService, MemoryJournal, dict]:
    rng = random.Random(seed)
    bars = random_bars(rng, n)
    strat = random_script(rng, bars, n)
    missing = {(i, k[1]) for i in range(1, n) for k in SYMBOLS if rng.random() < 0.03}
    svc, journal = new_service(strat)
    drive(svc, bars, list(range(n)), missing=missing)
    return svc, journal, {"bars": bars, "missing": missing, "strategy": strat}


# ---------------------------------------------------------------- property checks
def check_replay_parity(seed: int) -> list[str]:
    svc, journal, _ = run_random_session(seed)
    problems: list[str] = []
    if svc.fault is not None:
        problems.append(f"faulted: {svc.fault}")
    rep = replay(config(), journal.batches())
    if rep.ledger_hashes() != svc.ledger_hashes():
        problems.append("ledger hashes differ")
    if rep.fingerprint() != svc.session.fingerprint():
        problems.append("fingerprint differs")
    for name, book in svc.session.sim.books.items():
        report = book.ledger.reconcile()
        if not report.ok:
            problems.append(f"{name}: {report.problems}")
        if book.cash < 0:
            problems.append(f"{name}: negative cash")
    for s in svc.session.lifecycle.signals():
        if (
            s.status is SignalStatus.OPEN
            and svc.session.sim.books["baseline"].holder(Market(s.market), s.symbol) != s.signal_id
        ):
            problems.append(f"{s.signal_id} OPEN without a baseline position")
    return problems


def check_restore_equivalence(seed: int, fail_batch: int) -> list[str]:
    ref, ref_journal, ctx = run_random_session(seed)
    total = len(ref_journal.batches())
    fail_at = fail_batch % max(total, 1)
    rng = random.Random(seed)
    bars = random_bars(rng, 40)
    strat = random_script(rng, bars, 40)
    journal = MemoryJournal(fail_when=lambda i, _b: i == fail_at)
    svc, _ = new_service(strat, journal=journal)
    drive(svc, bars, list(range(40)), missing=ctx["missing"])
    if svc.fault is None:
        return ["journal failure was not detected"]
    journal.fail_when = None
    restored = PaperTradingService.restore(config(), journal, strategy=strat, tracker=svc.tracker)
    last = restored.session.last_minute_ms
    start = 0 if last is None else (last - minute(0)) // 60_000
    drive(restored, bars, list(range(start, 40)), missing=ctx["missing"])
    problems = []
    if restored.ledger_hashes() != ref.ledger_hashes():
        problems.append(f"ledgers differ after restore at batch {fail_at}")
    return problems


def check_live_clock_order(seed: int, settle_ms: int = 30_000) -> list[str]:
    """Any arrival order in which each bar arrives before its minute's deadline releases exactly
    the historical clock order of all delivered bars."""
    rng = random.Random(seed)
    n = 12
    keys = [BTC, ETH, (Market.USDM, "BTCUSDT")]
    arrivals: list[tuple[int, Bar]] = []
    delivered: list[Bar] = []
    for i in range(n):
        for key in keys:
            if rng.random() < 0.1:
                continue
            b = bar(i, 100, 101, 99, 100, symbol=key[1], market=key[0])
            close = b.close_time_ms
            arrivals.append((close + 1 + rng.randint(0, settle_ms - 1), b))
            delivered.append(b)
    arrivals.sort(key=lambda t: t[0])
    clock = LiveClock(keys, settle_ms=settle_ms)
    released: list[Bar] = []
    for t, b in arrivals:
        if clock.offer(b) != "buffered":
            return [f"unexpected offer result for {order_key(b)}"]
        released.extend(clock.release(t))
    released.extend(clock.release(minute(n + 10)))
    expected = list(HistoricalClock(delivered))
    return [] if released == expected and not clock.late else ["release order differs"]


def check_risk_engine_fail_closed(seed: int) -> list[str]:
    """Deterministic, and never PASSED when any market input is missing or after the cutoff."""
    import sys
    from dataclasses import replace
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "risk"))
    from risk_helpers import DECISION, check, view

    rng = random.Random(seed)
    engine = RiskEngine(RiskConfig(), CostModel())
    base = view()
    sym = base.symbols[(Market.SPOT, "BTCUSDT")]
    field = rng.choice(
        ["spread_bps", "last_bar_close_ms", "last_close", "offset", "as_of", "stream", "none"]
    )
    v: MarketView | None
    if field == "none":
        v = None
    elif field == "offset":
        v = replace(base, clock_offset_ms=None)
    elif field == "as_of":
        v = replace(base, as_of_ms=DECISION + rng.randint(1, 10_000))
    elif field == "stream":
        v = view(stream_count=0)
    else:
        broken: SymbolMarket = replace(sym, **{field: None})
        v = MarketView(base.as_of_ms, base.clock_offset_ms, {(Market.SPOT, "BTCUSDT"): broken})
    equity = D(rng.randint(5_000, 20_000))
    ck = check(equity=equity, peak_equity=equity, start_of_day_equity=equity)
    if engine.evaluate(ck, base).status is not RiskStatus.PASSED:
        return ["the complete baseline view must pass"]
    first, second = engine.evaluate(ck, v), engine.evaluate(ck, v)
    problems = [] if first == second else ["non-deterministic verdict"]
    if first.status is RiskStatus.PASSED:
        problems.append(f"passed with missing {field}")
    return problems


def seeds(n: int) -> Sequence[int]:
    return list(range(n))
