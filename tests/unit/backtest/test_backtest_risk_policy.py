"""Backtests apply the paper engine's risk policy (queue P1-6, D-E direction 2026-11-10).

Proves: (1) the policy rules are the paper engine's own functions, and on the same check and the
same market inputs a backtest check gives the same verdict as the paper engine minus the four
operational rules; (2) the market view never uses a bar closing after the check's cutoff;
(3) configuration fails closed and the risk config enters the backtest config hash by content;
(4) end to end, a live-policy backtest trades only what the paper baseline book accepts.
"""

from __future__ import annotations

import dataclasses
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from bt_helpers import (
    MIN,
    TEST_CONFIG,
    fast_strategy,
    make_env,
    ms,
    request,
    synthetic_rows,
    write_lake,
)

from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.research import risk_policy as rp
from tsclaude.research.runner import BacktestConfig, run_backtest
from tsclaude.risk.config import load_risk_config
from tsclaude.risk.engine import RULES, RiskEngine, Rule
from tsclaude.risk.market import MarketView, SymbolMarket, cutoff_ms
from tsclaude.sim.book import PositionView, RiskCheck
from tsclaude.sim.costs import CostModel
from tsclaude.sim.orders import EntryRequest

REPO = Path(__file__).resolve().parents[3]
RISK_YAML = REPO / "configs" / "risk.yaml"
RISK = load_risk_config(RISK_YAML)
COSTS = CostModel()
T0 = ms(2024, 3, 5, 12) - 1  # a decision close: the close_time of the 11:59 bar


def _bar(open_ms: int, close: float, symbol: str = "BTCUSDT", qv: float = 5e6) -> Bar:
    return Bar(
        market=Market.SPOT, symbol=symbol, interval=Interval("1m"), open_time_ms=open_ms,
        close_time_ms=open_ms + MIN - 1, open=close, high=close, low=close, close=close,
        volume=qv / close, quote_volume=qv, trades=10, taker_buy_volume=1.0,
        taker_buy_quote_volume=close, source="archive", ingested_at_ms=open_ms + MIN,
    )  # fmt: skip


def _request(stop: str, tp: str | None, symbol: str = "BTCUSDT") -> EntryRequest:
    return EntryRequest(
        request_id="r1", market=Market.SPOT, symbol=symbol, side=Side.LONG,
        kind=EntryRuleKind.MARKET_NEXT_OPEN, stop_price=Decimal(stop),
        take_profit_price=None if tp is None else Decimal(tp), valid_until_ms=T0 + 30 * MIN,
    )  # fmt: skip


# ---------------------------------------------------------------- (1) parity with Paper


def test_policy_rules_are_the_paper_engines_functions_minus_operational() -> None:
    assert [r for r, _ in rp.POLICY_RULES] == [r for r, _ in RULES if r not in rp.OPERATIONAL_RULES]
    paper = dict(RULES)
    assert all(fn is paper[r] for r, fn in rp.POLICY_RULES)  # the same objects, not copies
    # data_freshness applies (its bar checks are evaluated from history: closure review F1)
    assert {Rule.MARKET_VIEW, Rule.CLOCK_SKEW, Rule.SPREAD} == rp.OPERATIONAL_RULES
    assert Rule.DATA_FRESHNESS in dict(rp.POLICY_RULES)
    assert set(rp.SUBSTITUTIONS) == {str(r) for r in rp.OPERATIONAL_RULES}


def _random_check(rng: random.Random) -> RiskCheck:
    price = 100.0
    stop_frac = rng.choice([0.002, 0.005, 0.013, 0.02, 0.05])
    tp_r = rng.choice([0.5, 1.0, 1.5, 1.75, 2.0, 3.0])
    stop = f"{price * (1 - stop_frac):.8f}"
    tp = None if rng.random() < 0.1 else f"{price * (1 + tp_r * stop_frac):.8f}"
    equity = Decimal(rng.choice(["10000", "9700", "8900", "0"]))
    n_pos = rng.choice([0, 1, 3, 5])
    syms = ["ETHUSDT", "DOGEUSDT", "XRPUSDT", "BTCUSDT", "SOLUSDT"]
    positions = tuple(
        PositionView(
            market=Market.SPOT,
            symbol=syms[i],
            side=Side.LONG,
            qty=Decimal("1"),
            entry_price=Decimal("100"),
            entry_notional=Decimal(rng.choice(["500", "3000"])),
            mark_price=Decimal("100"),
        )
        for i in range(n_pos)
    )
    stage = rng.choice(["submit", "fill"])
    return RiskCheck(
        stage=stage, book="baseline", ts_ms=T0 if stage == "submit" else T0 + 1,
        request=_request(stop, tp), equity=equity, open_positions=n_pos, positions=positions,
        start_of_day_equity=Decimal("10000"), peak_equity=Decimal(rng.choice(["10000", "11500"])),
        earliest_exec_ms=T0 + 1, risk_per_trade=Decimal(rng.choice(["0.005", "0.01"])),
        max_notional_fraction=Decimal("1"),
        day_low_equity=Decimal(rng.choice(["10000", "9790"])),
    )  # fmt: skip


def test_backtest_verdicts_equal_the_paper_engines_on_the_same_inputs() -> None:
    """The paper engine, given a healthy live view with the same last bar, and the backtest hook
    reach the same verdict: the same policy reasons, and allow iff no policy rule fails."""
    paper = RiskEngine(RISK, COSTS)
    rng = random.Random(7)
    seen: set[str] = set()
    for _ in range(400):
        check = _random_check(rng)
        views = rp.BarViews()
        views.observe(_bar(T0 - MIN + 1, 100.0))  # closes at T0: the submit and fill cutoff bar
        hook = rp.BacktestRiskHook(RISK, COSTS, views)
        mine = hook.engine.evaluate(check, views.view(check))
        bt_view = views.view(check)
        assert bt_view is not None
        sym = bt_view.symbols[(Market.SPOT, "BTCUSDT")]
        live_sym = dataclasses.replace(
            sym, spread_bps=Decimal("1"), spread_as_of_ms=sym.last_bar_close_ms,
            median_spread_bps=Decimal("1"), spread_samples=60,
        )  # fmt: skip
        live_view = MarketView(
            as_of_ms=bt_view.as_of_ms, clock_offset_ms=0,
            symbols={(Market.SPOT, "BTCUSDT"): live_sym},
        )  # fmt: skip
        theirs = paper.evaluate(check, live_view)
        operational = tuple(r for r in theirs.reasons if r.split(":")[0] in
                            {str(x) for x in rp.OPERATIONAL_RULES})  # fmt: skip
        assert operational == (), operational  # the healthy view passes every operational rule
        assert mine.reasons == theirs.reasons
        assert hook(check).allow == theirs.allowed
        seen.update(r.split(":")[0] for r in mine.reasons)
    # the random checks exercised the policy rules, not only the happy path
    assert {"reward_risk", "risk_per_trade", "max_positions", "daily_loss", "drawdown"} <= seen


def test_the_15_floor_is_the_paper_floor() -> None:
    views = rp.BarViews()
    views.observe(_bar(T0 - MIN + 1, 100.0))
    hook = rp.BacktestRiskHook(RISK, COSTS, views)

    def check(stop: str, tp: str) -> RiskCheck:
        return RiskCheck(
            stage="submit", book="baseline", ts_ms=T0, request=_request(stop, tp),
            equity=Decimal("10000"), open_positions=0, start_of_day_equity=Decimal("10000"),
            peak_equity=Decimal("10000"), earliest_exec_ms=T0 + 1,
            risk_per_trade=Decimal("0.005"), max_notional_fraction=Decimal("1"),
        )  # fmt: skip

    # stop 1.3 % (H1's smallest), tp 2 R -> net RR 1.519 >= 1.5; tp 1.5 R -> 1.10 < 1.5
    assert hook(check("98.7", "102.6")).allow
    blocked = hook(check("98.7", "101.95"))
    assert not blocked.allow
    assert "reward_risk:rr_net:" in blocked.reason
    assert hook.blocked_by_rule == {"reward_risk": 1}


# ---------------------------------------------------------------- (2) no look-ahead


def test_views_never_use_a_bar_closing_after_the_cutoff() -> None:
    views = rp.BarViews()
    views.observe(_bar(T0 - 2 * MIN + 1, 100.0))  # closes T0 - 1 min
    views.observe(_bar(T0 - MIN + 1, 101.0))  # closes at T0
    views.observe(_bar(T0 + 1, 150.0))  # the executing bar: opens T0 + 1, closes after
    req = _request("98", "105")
    submit = RiskCheck("submit", "baseline", T0, req, Decimal("10000"), 0)
    fill = RiskCheck("fill", "baseline", T0 + 1, req, Decimal("10000"), 0)
    for check in (submit, fill):
        v = views.view(check)
        assert v is not None
        sym = v.symbols[(Market.SPOT, "BTCUSDT")]
        assert sym.last_close == Decimal("101")
        assert sym.last_bar_close_ms == T0
        assert v.as_of_ms <= T0  # the submit and the fill cutoff are both T0 here
    early = RiskCheck("submit", "baseline", T0 - 2 * MIN, req, Decimal("10000"), 0)
    assert views.view(early) is None  # nothing closed yet at that cutoff: no reference price
    other = RiskCheck("submit", "baseline", T0, _request("98", "105", "ETHUSDT"),
                      Decimal("10000"), 0)  # fmt: skip
    assert views.view(other) is None  # another symbol's bars are never used
    with pytest.raises(ValueError, match="out of order"):
        views.observe(_bar(T0 - MIN + 1, 99.0))


def test_a_missing_view_blocks_the_entry() -> None:
    hook = rp.BacktestRiskHook(RISK, COSTS, rp.BarViews())
    d = hook(RiskCheck("submit", "baseline", T0, _request("98", "110"), Decimal("10000"), 0,
                       start_of_day_equity=Decimal("10000"), peak_equity=Decimal("10000"),
                       earliest_exec_ms=T0 + 1, risk_per_trade=Decimal("0.005"),
                       max_notional_fraction=Decimal("1")))  # fmt: skip
    assert not d.allow
    assert "rr_undefined:no_reference_price" in d.reason


# ---------------------------------------------------------------- (3) configuration


def _cfg_dir(tmp_path: Path, risk_edit: dict[str, Any] | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(RISK_YAML.read_text(encoding="utf-8"))
    raw.update(risk_edit or {})
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (tmp_path / "backtest.yaml").write_text(
        yaml.safe_dump({"risk_policy": "live", "risk_config": "risk.yaml"}), encoding="utf-8"
    )
    return tmp_path / "backtest.yaml"


def test_config_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="risk_policy must be one of"):
        BacktestConfig.from_mapping({"risk_policy": "paper"})
    with pytest.raises(ValueError, match="needs risk_config"):
        BacktestConfig.from_mapping({"risk_policy": "live"})
    with pytest.raises(ValueError, match="needs the config file's directory"):
        BacktestConfig.from_mapping({"risk_policy": "live", "risk_config": "risk.yaml"})
    with pytest.raises(ValueError, match="risk_config:"):
        BacktestConfig.from_mapping(
            {"risk_policy": "live", "risk_config": "nope.yaml"}, base_dir=tmp_path
        )
    with pytest.raises(ValueError, match="only used with risk_policy live"):
        BacktestConfig.from_mapping({"risk_config": "risk.yaml"})
    with pytest.raises(ValueError, match="exceeds the risk policy"):
        BacktestConfig.from_mapping(
            {"risk_policy": "live", "risk_config": str(RISK_YAML), "risk_per_trade": "0.01"}
        )


def test_risk_config_enters_the_hash_by_content(tmp_path: Path) -> None:
    allow = BacktestConfig()
    assert "risk_policy" not in allow.to_dict()  # allow_all configs keep their hash
    a = BacktestConfig.load(_cfg_dir(tmp_path / "a"))
    b = BacktestConfig.load(_cfg_dir(tmp_path / "b"))
    c = BacktestConfig.load(_cfg_dir(tmp_path / "c", {"min_rr_net": "1.6"}))
    assert a.config_hash == b.config_hash != allow.config_hash  # same content, other folder
    assert c.config_hash != a.config_hash  # a changed floor changes the pinned config hash
    desc = a.to_dict()["risk_policy"]
    assert desc["mode"] == "live"
    assert desc["risk_config_hash"] == RISK.config_hash
    assert "path" not in json.dumps(desc)


def test_the_repo_backtest_config_uses_the_live_policy() -> None:
    cfg = BacktestConfig.load(REPO / "configs" / "backtest.yaml")
    assert cfg.risk_policy == rp.LIVE
    assert cfg.risk is not None
    assert cfg.risk.config_hash == RISK.config_hash
    assert cfg.risk.min_rr_net == Decimal("1.5")


# ---------------------------------------------------------------- (4) end to end

START, END = ms(2024, 3, 1), ms(2024, 3, 15)
FROM, TO = ms(2024, 3, 3), ms(2024, 3, 15)


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("rp-lake")
    write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(START, END))])
    return d


def _live_cfg() -> BacktestConfig:
    raw = {k: v for k, v in TEST_CONFIG.to_dict().items() if k != "risk_policy"}
    return BacktestConfig.from_mapping({**raw, "risk_policy": "live",
                                        "risk_config": str(RISK_YAML)})  # fmt: skip


def test_live_policy_backtest_trades_only_what_paper_accepts(lake: Path) -> None:
    live = _live_cfg()
    # 2 R with tight stops: only the trades whose stop is wide enough pass the 1.5 floor
    strat = fast_strategy(tp_r=2.0, stop_atr=0.75)
    allow = run_backtest(request(FROM, TO, strategy=strat), make_env(lake), TEST_CONFIG)
    out = run_backtest(request(FROM, TO, strategy=strat), make_env(lake), live)
    manifest = json.loads((out.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["risk_hook"]["mode"] == "live"
    assert manifest["risk_hook"]["risk_config_hash"] == RISK.config_hash
    assert manifest["config_hash"] == live.config_hash != TEST_CONFIG.config_hash
    policy = out.metrics["integrity"]["risk_policy"]
    assert policy["mode"] == "live"
    assert policy["checks"] > 0
    n_live, n_allow = out.metrics["trades"]["n"], allow.metrics["trades"]["n"]
    assert 0 < n_live < n_allow, (n_live, n_allow)  # some small stops fail the 1.5 floor
    assert policy["blocked_by_rule"].get("reward_risk", 0) > 0
    # every booked trade passes the paper engine's reward/risk rule at its own planned levels
    from tsclaude.research.parquet import read_table

    _, rows = read_table(out.run_dir / "trades.parquet")
    for row in rows:
        if row["book"] != "baseline":
            continue
        req = EntryRequest(
            request_id="t", market=Market.SPOT, symbol="BTCUSDT", side=Side.LONG,
            kind=EntryRuleKind.MARKET_NEXT_OPEN, stop_price=Decimal(str(row["stop_price"])),
            take_profit_price=Decimal(str(row["take_profit_price"])), valid_until_ms=1,
        )  # fmt: skip
        # the decision-time reference is the close before the entry bar; bound it by the fill
        ref = Decimal(str(row["entry_price"]))
        sym = SymbolMarket(Market.SPOT, "BTCUSDT", (), 1, 0, 0, ref, None, None, None, None, 0)
        from tsclaude.risk.engine import cost_adjusted_rr

        chk = RiskCheck("submit", "baseline", 0, req, Decimal("10000"), 0)
        value, _ = cost_adjusted_rr(chk, MarketView(0, 0, {(Market.SPOT, "BTCUSDT"): sym}), COSTS)
        assert value is not None
        # the entry fill can differ from the decision close by the next bar's open move, so
        # allow a small tolerance; a 1.0-R trade (rr ~ 0.5) could never come close to 1.5
        assert value >= Decimal("1.3"), (row, value)


def test_live_policy_blocks_every_trade_of_a_geometry_below_the_floor(lake: Path) -> None:
    out = run_backtest(request(FROM, TO, strategy=fast_strategy(tp_r=1.0)), make_env(lake),
                       _live_cfg())  # fmt: skip
    assert out.metrics["trades"]["n"] == 0
    policy = out.metrics["integrity"]["risk_policy"]
    assert policy["blocked_by_rule"]["reward_risk"] == policy["checks"] > 0
    assert all("reward_risk" in k for k in out.metrics["integrity"]["rejections"])


def test_walk_forward_reports_name_the_policy_their_runs_applied(lake: Path) -> None:
    from tsclaude.research import walkforward as wf

    live = _live_cfg()
    assert wf._describe_policy(live)["mode"] == "live"
    assert wf._describe_policy(TEST_CONFIG)["mode"] == "allow_all"
    grid = {"a": {"tp_r": 1.0}, "b": {"tp_r": 2.0}}
    symbols = [(Market.SPOT, "BTCUSDT")]
    env = make_env(lake)
    on = wf._live_compatibility(env, "bracket_reference", grid, symbols, RISK, policy_live=True)
    off = wf._live_compatibility(env, "bracket_reference", grid, symbols, RISK)
    assert on is not None
    assert off is not None
    assert "applied the paper risk policy" in on["note"]
    assert "applied no risk gate" in off["note"]
    assert on["configurations"]["a"]["status"] == "INCOMPATIBLE"
    assert on["configurations"]["b"]["status"] == "COMPATIBLE"


# ---------------------------------------------------------------- closure review F1, F2, F10


def _tracker_view(bars: list[Bar], cutoff: int) -> SymbolMarket:
    """The paper tracker's view of the same bars (a healthy snapshot just before the cutoff)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "risk"))
    from risk_helpers import snapshot

    from tsclaude.risk.market import MarketStateTracker

    tr = MarketStateTracker()
    tr.observe_snapshot(snapshot(cutoff - 500))
    for b in bars:
        tr.observe_bar(b)
    v = tr.view_at(cutoff, [(Market.SPOT, "BTCUSDT")])
    assert v is not None
    return v.symbols[(Market.SPOT, "BTCUSDT")]


@pytest.mark.parametrize("skip", [(), (100,), (100, 101, 102), (5,), (299,)])
def test_bar_views_equal_the_paper_trackers_bar_fields(skip: tuple[int, ...]) -> None:
    start = T0 + 1 - 300 * MIN
    bars = [_bar(start + i * MIN, 100.0 + i / 10, qv=1e6 + i) for i in range(300) if i not in skip]
    views = rp.BarViews(RISK.data_freshness.lookback_minutes)
    for b in bars:
        views.observe(b)
    for stage, ts in (("submit", T0), ("fill", T0 + 1), ("submit", T0 - 30 * MIN)):
        check = RiskCheck(stage, "baseline", ts, _request("98", "110"), Decimal("10000"), 0)
        mine = views.view(check)
        assert mine is not None
        ours = mine.symbols[(Market.SPOT, "BTCUSDT")]
        theirs = _tracker_view(bars, cutoff_ms(stage, ts))
        for f in ("last_bar_close_ms", "last_close", "last_quote_volume", "lookback_gap_minutes"):
            assert getattr(ours, f) == getattr(theirs, f), (f, skip, stage, ts)


def test_a_lookback_into_pruned_history_fails_closed() -> None:
    views = rp.BarViews(240)
    start = T0 + 1 - 400 * MIN
    for i in range(400):
        views.observe(_bar(start + i * MIN, 100.0))
    # 400 bars observed, about 300 kept: a cutoff whose lookback needs dropped bars gets no view
    far = RiskCheck("submit", "baseline", T0 - 120 * MIN, _request("98", "110"), Decimal("1"), 0)
    assert views.view(far) is None
    near = RiskCheck("submit", "baseline", T0, _request("98", "110"), Decimal("1"), 0)
    assert views.view(near) is not None


def _policy_check(stage: str, ts: int) -> RiskCheck:
    return RiskCheck(
        stage=stage, book="baseline", ts_ms=ts, request=_request("98.7", "102.6"),
        equity=Decimal("10000"), open_positions=0, start_of_day_equity=Decimal("10000"),
        peak_equity=Decimal("10000"), earliest_exec_ms=T0 + 1, risk_per_trade=Decimal("0.005"),
        max_notional_fraction=Decimal("1"),
    )  # fmt: skip


def test_stale_bars_and_lookback_gaps_block_as_in_paper() -> None:
    """F1: the bar checks of data_freshness apply in backtests as in Paper."""
    paper = RiskEngine(RISK, COSTS)
    old = rp.BarViews()
    old.observe(_bar(T0 + 1 - 240 * MIN, 100.0))  # the last bar closed 239 min before T0
    hook = rp.BacktestRiskHook(RISK, COSTS, old)
    d = hook(_policy_check("submit", T0))
    assert not d.allow
    assert "data_freshness:" in d.reason
    view = old.view(_policy_check("submit", T0))
    assert view is not None
    assert "data_freshness:bar_stale" in "|".join(paper.evaluate(_policy_check("submit", T0),
                                                                 view).reasons)  # fmt: skip
    gap = rp.BarViews()
    for i in range(240):
        if i != 120:  # one missing minute two hours before the decision
            gap.observe(_bar(T0 + 1 - (240 - i) * MIN, 100.0))
    d = rp.BacktestRiskHook(RISK, COSTS, gap)(_policy_check("submit", T0))
    assert not d.allow
    assert "data_freshness:gap_in_lookback:1m" in d.reason
    fresh = rp.BarViews()
    for i in range(240):
        fresh.observe(_bar(T0 + 1 - (240 - i) * MIN, 100.0))
    assert rp.BacktestRiskHook(RISK, COSTS, fresh)(_policy_check("submit", T0)).allow


def test_the_submit_reference_is_the_decision_bars_close(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2a: bars are observed before the step, so a submit check at the decision close sees that
    bar (and a fill check the close before the executing bar)."""
    seen: list[tuple[str, int, int | None]] = []
    real = rp.BacktestRiskHook.__call__

    def spy(self: rp.BacktestRiskHook, check: RiskCheck) -> Any:
        v = self.views.view(check)
        last = None if v is None else v.symbols[(Market.SPOT, "BTCUSDT")].last_bar_close_ms
        seen.append((check.stage, check.ts_ms, last))
        return real(self, check)

    monkeypatch.setattr(rp.BacktestRiskHook, "__call__", spy)
    run_backtest(request(FROM, TO, strategy=fast_strategy(tp_r=3.0)), make_env(lake), _live_cfg())
    submits = [s for s in seen if s[0] == "submit"]
    fills = [s for s in seen if s[0] == "fill"]
    assert submits
    assert fills
    assert all(last == ts for _, ts, last in submits)  # decision latency 0: the decision close
    assert all(last == ts - 1 for _, ts, last in fills)  # the close before the executing open


def test_live_books_get_the_kill_switch_and_the_base_cost_model(lake: Path) -> None:
    """F2b, F2c: every live-policy book halts at the paper kill switch, and the engine judges
    every book (cost stress included) with the base cost model, as the live engine does."""
    from decimal import Decimal as D

    from tsclaude.research import runner

    env = make_env(lake)
    cfg = _live_cfg()
    req = request(FROM, TO)
    names = [("baseline", env.cost_model), ("stress_x2", env.cost_model.scaled(D("2")))]
    books = runner._books(env, req, cfg, names, rp.BarViews())
    assert [b.kill_switch_drawdown for b in books] == [RISK.drawdown_kill_switch] * 2
    hooks = [b.risk_hook for b in books]
    assert all(isinstance(h, rp.BacktestRiskHook) for h in hooks)
    assert all(h.engine.cost_model is env.cost_model for h in hooks)  # type: ignore[union-attr]
    assert books[1].cost_model is not env.cost_model  # the stress book still pays stressed costs
    with pytest.raises(ValueError, match="needs the run's bar views"):
        runner._books(env, req, cfg, names, None)
    allow = runner._books(env, req, TEST_CONFIG, names)
    assert all(b.kill_switch_drawdown is None for b in allow)


@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="needs git")
def test_a_run_of_a_keep_floor_preregistration_rechecks_policy_and_costs(
    lake: Path, tmp_path: Path
) -> None:
    """Run side (closure review F6): a frozen preregistration that keeps the live floor is run only
    with backtests applying the live policy, and its configurations are re-checked at the run's
    own cost model, before anything runs."""
    import subprocess
    from datetime import UTC, datetime

    from tsclaude.research import preregistration as pr
    from tsclaude.research import walkforward as wf

    repo = tmp_path / "repo"
    (repo / "preregistrations").mkdir(parents=True)
    doc = pr.load(REPO / "preregistrations" / "TEMPLATE.yaml")  # bracket_reference, 2 R and 3 R
    doc.update(prereg_id="H-FLOOR", holdout_procedure="two-stage-selection-record",
               risk_compatibility=pr.KEEP_LIVE_FLOOR)  # fmt: skip
    doc["trial_budget"]["counting"] = "configurations"
    doc["universe"]["symbols"] = ["BTCUSDT"]
    doc["walk_forward"].update(anchor="2023-01-01", end="2023-04-01", initial_train_months=1,
                               test_months=1)  # fmt: skip
    path = repo / "preregistrations" / "H-FLOOR.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    live = _live_cfg()
    pr.freeze(path, now_ms=0, code={"commit": "t", "dirty": False},
              backtest_config_hash=live.config_hash, backtest_risk=live.risk)  # fmt: skip
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t",
                                                "commit", "-q", "-m", "p"]):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    frozen = pr.load(path)
    grid = {wf.config_id_of(p): p for p in frozen["search_space"]}

    def ms(y: int, m: int) -> int:
        return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)

    folds = wf.plan_folds(ms(2023, 1), ms(2023, 4), initial_train_months=1, test_months=1,
                          embargo_ms=1440 * MIN)  # fmt: skip
    env = dataclasses.replace(make_env(lake), repo_root=repo)

    def conform(env: Any, risk: Any) -> Any:
        return wf._prereg_conformance(
            env, "H-FLOOR", strategy_id="bracket_reference", grid=grid,
            symbols=[(Market.SPOT, "BTCUSDT")], folds=folds, n_min=300, min_oos_coverage=1.0,
            dataset_ids=None, final_selection=True, config_hash=live.config_hash, risk=risk,
        )  # fmt: skip

    with pytest.raises(wf.WalkForwardError, match="needs backtests with risk_policy live"):
        conform(env, None)
    # scaling every cost leaves H1-style geometries unchanged (the cost filter scales the smallest
    # stop with them); take-profits filled as taker with slippage do not: 2 R falls to 1.4995
    costly = dataclasses.replace(
        env, cost_model=dataclasses.replace(env.cost_model, tp_as_taker=True)
    )
    with pytest.raises(wf.WalkForwardError, match=r"with the live floor min_rr_net 1\.5"):
        conform(costly, live.risk)
    assert conform(env, live.risk) is not None  # the preregistered run itself conforms
