"""Risk config loading (strict, versioned) and the as-of-cutoff market tracker."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml
from risk_helpers import check, snapshot

from tsclaude.contracts.market import Market, StreamState
from tsclaude.risk.config import RiskConfig, RiskConfigError, config_from_mapping, load_risk_config
from tsclaude.risk.engine import RiskEngine
from tsclaude.risk.hook import EngineRiskHook, RecordedViews, ReplayMismatch, tracker_provider
from tsclaude.risk.market import MarketStateTracker, MarketView, cutoff_ms, spread_bps
from tsclaude.sim.costs import CostModel
from tsclaude.sim.testing import bar, minute

REPO = Path(__file__).resolve().parents[3]
KEY = (Market.SPOT, "BTCUSDT")


# ---------------------------------------------------------------- config
def test_repository_config_matches_documented_defaults() -> None:
    cfg = load_risk_config(REPO / "configs" / "risk.yaml")
    assert cfg == RiskConfig()
    assert cfg.version == "risk-config-v1"
    assert cfg.max_positions == 5
    assert cfg.drawdown_kill_switch == D("0.10")
    assert len(cfg.config_hash) == 64


def _raw() -> dict[str, object]:
    raw = yaml.safe_load((REPO / "configs" / "risk.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r.update(extra=1), "unknown keys"),
        (lambda r: r.pop("max_positions"), "missing keys"),
        (lambda r: r.update(max_positions=0), "max_positions"),
        (lambda r: r.update(daily_loss_limit="0"), "daily_loss_limit"),
        (lambda r: r.update(max_gross_exposure="1.5"), "no leverage"),
        (lambda r: r.update(min_rr_net=1.5), "quoted decimal"),
        (lambda r: r.update(min_rr_net="NaN"), "finite"),
        (lambda r: r.update(require_take_profit="yes"), "true or false"),
        (lambda r: r["spread"].update(cap_bps="-1"), "cap_bps"),  # type: ignore[index]
    ],
)
def test_invalid_config_fails_closed(mutate: object, match: str) -> None:
    raw = _raw()
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(RiskConfigError, match=match):
        config_from_mapping(raw)


def test_missing_or_empty_config_file_fails(tmp_path: Path) -> None:
    with pytest.raises(RiskConfigError):
        load_risk_config(tmp_path / "nope.yaml")
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
    with pytest.raises(RiskConfigError, match="empty"):
        load_risk_config(tmp_path / "empty.yaml")


def test_config_hash_changes_with_any_value() -> None:
    assert RiskConfig().config_hash != RiskConfig(max_positions=4).config_hash


# ---------------------------------------------------------------- tracker
def test_cutoff_is_decision_close_for_submit_and_previous_close_for_fill() -> None:
    assert cutoff_ms("submit", minute(5) - 1) == minute(5) - 1
    assert cutoff_ms("fill", minute(5)) == minute(5) - 1
    with pytest.raises(ValueError, match="stage"):
        cutoff_ms("later", 0)


def test_spread_bps_exact_and_rejects_crossed_books() -> None:
    assert spread_bps(999.95, 1000.05) == D("1.00000000")
    with pytest.raises(ValueError, match="invalid"):
        spread_bps(1000.1, 1000.0)


def test_view_uses_only_observations_at_or_before_the_cutoff() -> None:
    tr = MarketStateTracker(min_samples=1)
    t = minute(10) - 1
    tr.observe_snapshot(snapshot(t - 1_000, bid=999.95, ask=1000.05))
    tr.observe_bar(bar(9, 1000, 1001, 999, 1000))  # closes at minute(10) - 1 = t
    tr.observe_snapshot(snapshot(t + 1_000, bid=999.0, ask=1001.0))  # after the cutoff
    tr.observe_bar(bar(10, 1000, 1006, 999, 1005))  # closes after the cutoff
    v = tr.view_at(t, [KEY])
    assert v is not None
    sym = v.get(*KEY)
    assert sym is not None
    assert v.as_of_ms == t - 1_000
    assert sym.spread_bps == D("1.00000000")  # not the later 20 bps sample
    assert sym.last_close == D("1000")
    assert sym.last_bar_close_ms == t
    assert sym.streams_healthy
    assert v.clock_offset_ms == 15
    assert tr.view_at(t - 2_000, [KEY]) is None  # nothing observed yet


def test_later_report_of_unhealthy_since_before_cutoff_makes_view_unhealthy() -> None:
    """Conservative merge: a stream reported STALE *since* a time <= cutoff counts as unhealthy at
    the cutoff even though the snapshot before the cutoff still said HEALTHY."""
    tr = MarketStateTracker()
    t = minute(10) - 1
    tr.observe_snapshot(snapshot(t - 500))
    stale = {
        "kline_1m": (StreamState.STALE, t - 200),
        "book_ticker": (StreamState.HEALTHY, t - 600_000),
    }
    tr.observe_snapshot(snapshot(t + 2_000, states=stale))
    v = tr.view_at(t, [KEY])
    assert v is not None
    sym = v.get(*KEY)
    assert sym is not None
    assert sym.unhealthy_streams == ("spot:BTCUSDT:kline_1m",)
    # ... but a STALE state that began after the cutoff does not reach back in time
    tr2 = MarketStateTracker()
    tr2.observe_snapshot(snapshot(t - 500))
    late = {**stale, "kline_1m": (StreamState.STALE, t + 1_000)}
    tr2.observe_snapshot(snapshot(t + 2_000, states=late))
    v2 = tr2.view_at(t, [KEY])
    assert v2 is not None
    assert v2.symbols[KEY].streams_healthy


def test_service_not_running_means_unhealthy_and_unknown_skew() -> None:
    tr = MarketStateTracker()
    tr.observe_snapshot(snapshot(minute(1), running=False))
    v = tr.view_at(minute(1), [KEY])
    assert v is not None
    assert not v.symbols[KEY].streams_healthy
    assert v.clock_offset_ms is None


def test_median_needs_min_samples_and_uses_the_window() -> None:
    tr = MarketStateTracker(median_window_ms=10_000, min_samples=3)
    base = minute(100)
    spreads = [(0.05, 1.0), (0.05, 1.0), (0.10, 2.0), (0.30, 6.0)]  # half-width -> bps
    for i, (half, _) in enumerate(spreads):
        tr.observe_snapshot(snapshot(base + i * 3_000, bid=1000 - half, ask=1000 + half))
    v = tr.view_at(base + 9_000, [KEY])
    assert v is not None
    sym = v.symbols[KEY]
    # samples received at base-100, +2900, +5900, +8900; window (cutoff - 10 s, cutoff]
    assert sym.spread_samples == 4
    assert sym.median_spread_bps == D("1.50000000")
    assert sym.spread_bps == D("6.00000000")
    few = tr.view_at(base + 3_000, [KEY])
    assert few is not None
    assert few.symbols[KEY].median_spread_bps is None  # 2 samples < 3


def test_market_view_payload_round_trip() -> None:
    tr = MarketStateTracker(min_samples=1)
    tr.observe_snapshot(snapshot(minute(3)))
    tr.observe_bar(bar(2, 1000, 1001, 999, 1000.5))
    v = tr.view_at(minute(3), [KEY])
    assert v is not None
    assert MarketView.from_payload(v.to_payload()) == v


# ---------------------------------------------------------------- hook
def test_hook_records_and_replay_recomputes_identically() -> None:
    tr = MarketStateTracker(min_samples=1)
    t = minute(10) - 1
    tr.observe_snapshot(snapshot(t - 300))
    tr.observe_bar(bar(9, 1000, 1001, 999, 1000))
    engine = RiskEngine(RiskConfig(), CostModel())
    live = EngineRiskHook(engine, tracker_provider(tr))
    decision = live(check())
    assert decision.allow, decision.reason
    records = live.drain()
    assert len(records) == 1
    assert records[0]["allow"] is True
    replay = EngineRiskHook(engine, RecordedViews(records))
    assert replay(check()) == decision
    assert replay.drain()[0]["view"] == records[0]["view"]


def test_replay_mismatch_and_provider_errors_block() -> None:
    engine = RiskEngine(RiskConfig(), CostModel())
    hook = EngineRiskHook(engine, RecordedViews([]))
    d = hook(check())
    assert not d.allow
    assert hook.drain()[0]["provider_error"] == "ReplayMismatch"
    rec = {"stage": "fill", "book": "baseline", "request_id": "x", "ts_ms": 0, "view": None}
    views = RecordedViews([rec])
    with pytest.raises(ReplayMismatch):
        views(check())
