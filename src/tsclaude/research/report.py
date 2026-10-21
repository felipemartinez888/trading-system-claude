"""Backtest report: metrics (RESEARCH_PROTOCOL §4-§6, SIMULATION §8) and the evidence grade.

Everything here is a pure function of simulator outputs (trade records, equity points, book
summaries) and `certify` results, so the same inputs give byte-identical ``metrics.json``. Floats
are rounded to 12 significant digits before serialization so last-ulp differences between
platform math libraries cannot change the metrics hash; non-finite values become the strings
``"inf"`` / ``"-inf"`` / ``"nan"``.

Definitions (R = planned risk at the actual entry price, net of all costs; see `sim.book`):

- win rate ``p = wins / n`` with the exact two-sided Clopper-Pearson CI at ``alpha`` and the
  certified (multiple-testing adjusted) lower bound from `certify`;
- ``expectancy_r`` mean R; ``expectancy_pct`` mean of ``net_pnl / entry notional`` in %;
- ``profit_factor`` = sum of winning net P&L / |sum of losing net P&L| (None without losses);
- ``payoff_ratio`` = average win R / average loss R;
- ``cost_share`` = (fees + slippage) / sum over trades of |gross P&L before costs|;
- Sharpe / Sortino on **daily UTC equity returns** (last mark of each day), annualized with
  ``sqrt(365)`` (crypto trades every day); Sortino uses the root mean square of negative returns;
- ``turnover`` = traded notional (entries + exits) / starting equity;
- ``exposure`` = time in position summed over trades / (range length x symbols);
- ambiguity sensitivity = expectancy if every ``ambiguous_bar`` trade were resolved TP-first;
- random-entry control: same exits, same number of entries per (symbol, UTC hour); the entry
  edge is ``mean R(strategy) - mean R(control)`` with a one-sided Welch normal lower bound at
  ``alpha_adj``.

Grades (RESEARCH_PROTOCOL §6), the worst applicable wins:

- ``X`` integrity failure (data gaps beyond tolerance, timing/causality check failure, ledger not
  reconciled, signal-source errors, missing manifest fields);
- ``D`` rejected: certification evaluated with enough effective trades and a non-positive
  certified margin or expectancy bound, or a realized loss beyond the declared ``max_loss_r``;
- ``C`` insufficient: too few trades / n_eff, untrustworthy bootstrap, ambiguous or spans-gap
  share above the cap (default 5 %), ``max_loss_r`` not measured, development or mixed segment,
  or no demonstrated entry edge over the random control;
- ``B`` certified on validation (or on holdout without all A conditions); cost-stress failures
  cap at B;
- ``A`` certified on the holdout of a preregistered strategy, robust to cost stress
  (certified at x1.5 and positive mean at x2), beating its random control, **and** a recorded,
  verified validation run of the same configuration graded B and certified (audit N4).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np

from tsclaude.research.stats import Certification, clopper_pearson, norm_ppf
from tsclaude.sim.book import EquityPoint, TradeRecord
from tsclaude.sim.fills import ExitReason

GRADE_ORDER = {"X": 0, "D": 1, "C": 2, "B": 3, "A": 4}
REPORT_VERSION = "report-v1"
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


# ---------------------------------------------------------------- serialization helpers
def clean(value: Any) -> Any:
    """JSON-safe, platform-stable form (floats to 12 significant digits)."""
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float | np.floating):
        v = float(value)
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return float(f"{v:.12g}")
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): clean(v) for k, v in value.items()}
    if hasattr(value, "_asdict"):  # NamedTuple (stats Interval)
        return clean(value._asdict())
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    raise TypeError(f"cannot serialize {type(value).__name__}")


def certification_dict(cert: Certification) -> dict[str, Any]:
    out: dict[str, Any] = clean({f.name: getattr(cert, f.name) for f in fields(cert)})
    return out


# ---------------------------------------------------------------- trade statistics
def outcomes(trades: Sequence[TradeRecord]) -> list[float]:
    return [float(t.r_multiple) for t in trades]


def order_trades(trades: Sequence[TradeRecord]) -> list[TradeRecord]:
    """Chronological by entry (dependence estimates need time order)."""
    return sorted(trades, key=lambda t: (t.entry_bar_open_ms, str(t.market), t.symbol))


def _mean(xs: Sequence[float]) -> float | None:
    return float(np.mean(xs)) if xs else None


def trade_stats(trades: Sequence[TradeRecord], alpha: float) -> dict[str, Any]:
    r = outcomes(trades)
    n = len(r)
    wins = [x for x in r if x > 0]
    losses = [-x for x in r if x <= 0]
    win_pnl = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal(0))
    loss_pnl = -sum((t.net_pnl for t in trades if t.net_pnl < 0), Decimal(0))
    costs = sum((t.fees + t.slippage_cost for t in trades), Decimal(0))
    gross_abs = sum((abs(t.gross_pnl + t.slippage_cost) for t in trades), Decimal(0))
    pct = [float(t.net_pnl / (t.entry_price * t.qty)) * 100.0 for t in trades]
    ci = clopper_pearson(len(wins), n, alpha, "two-sided") if n else None
    alt = [
        float(t.alt_r_if_tp_first)
        if t.ambiguous_bar and t.alt_r_if_tp_first is not None
        else float(t.r_multiple)
        for t in trades
    ]
    flagged_amb = sum(1 for t in trades if t.ambiguous_bar)
    flagged_gap = sum(1 for t in trades if t.spans_gap)
    return {
        "n": n,
        "wins": len(wins),
        "win_rate": None if n == 0 else len(wins) / n,
        "win_rate_ci": None if ci is None else {"lower": ci.lower, "upper": ci.upper},
        "win_rate_ci_level": 1.0 - alpha,
        "expectancy_r": _mean(r),
        "expectancy_pct": _mean(pct),
        "avg_win_r": _mean(wins),
        "avg_loss_r": _mean(losses),
        "payoff_ratio": (
            None if not wins or not losses or np.mean(losses) == 0 else _ratio(wins, losses)
        ),
        "profit_factor": None if loss_pnl == 0 else float(win_pnl / loss_pnl),
        "worst_trade_r": min(r) if r else None,
        "best_trade_r": max(r) if r else None,
        "total_net_pnl": str(sum((t.net_pnl for t in trades), Decimal(0))),
        "total_fees": str(sum((t.fees for t in trades), Decimal(0))),
        "total_slippage": str(sum((t.slippage_cost for t in trades), Decimal(0))),
        "cost_share": None if gross_abs == 0 else float(costs / gross_abs),
        "ambiguous_count": flagged_amb,
        "ambiguous_share": None if n == 0 else flagged_amb / n,
        "spans_gap_count": flagged_gap,
        "spans_gap_share": None if n == 0 else flagged_gap / n,
        "expectancy_r_if_ambiguous_tp_first": _mean(alt),
        "gap_fill_count": sum(1 for t in trades if t.gap_fill),
        "mae_r_mean": _mean([float(t.mae_r) for t in trades]),
        "mfe_r_mean": _mean([float(t.mfe_r) for t in trades]),
        "exit_reasons": {
            str(reason): sum(1 for t in trades if t.exit_reason is reason) for reason in ExitReason
        },
    }


def _ratio(wins: Sequence[float], losses: Sequence[float]) -> float:
    return float(np.mean(wins) / np.mean(losses))


def breakdowns(trades: Sequence[TradeRecord]) -> dict[str, Any]:
    def group(key_of: Any) -> dict[str, Any]:
        groups: dict[str, list[float]] = {}
        for t in trades:
            groups.setdefault(key_of(t), []).append(float(t.r_multiple))
        return {
            k: {
                "n": len(v),
                "win_rate": sum(1 for x in v if x > 0) / len(v),
                "expectancy_r": float(np.mean(v)),
            }
            for k, v in sorted(groups.items())
        }

    return {
        "by_symbol": group(lambda t: f"{t.market}:{t.symbol}"),
        "by_entry_hour_utc": group(lambda t: f"{(t.entry_bar_open_ms // MS_PER_HOUR) % 24:02d}"),
        "by_side": group(lambda t: str(t.side)),
    }


def exposure_turnover(
    trades: Sequence[TradeRecord],
    starting_equity: Decimal,
    range_ms: int,
    n_symbols: int,
) -> dict[str, Any]:
    held = sum(t.exit_bar_open_ms + 60_000 - t.entry_bar_open_ms for t in trades)
    traded = sum((t.qty * (t.entry_price + t.exit_price) for t in trades), Decimal(0))
    denom = range_ms * max(1, n_symbols)
    return {
        "exposure": None if denom <= 0 else held / denom,
        "turnover": float(traded / starting_equity),
        "avg_holding_minutes": None if not trades else held / len(trades) / 60_000,
    }


def equity_stats(points: Sequence[EquityPoint], summary: Mapping[str, Any]) -> dict[str, Any]:
    daily: dict[int, Decimal] = {}
    for p in points:
        daily[p.ts_ms // MS_PER_DAY] = p.equity  # last mark of each UTC day
    eq = np.array([float(v) for _, v in sorted(daily.items())], dtype=np.float64)
    rets = eq[1:] / eq[:-1] - 1.0 if eq.size >= 2 else np.zeros(0)
    sharpe = sortino = None
    if rets.size >= 2:
        sd = float(np.std(rets, ddof=1))
        sharpe = None if sd == 0 else float(np.mean(rets)) / sd * math.sqrt(365.0)
        downside = float(np.sqrt(np.mean(np.minimum(rets, 0.0) ** 2)))
        sortino = None if downside == 0 else float(np.mean(rets)) / downside * math.sqrt(365.0)
    first = float(points[0].equity) if points else None
    last = float(points[-1].equity) if points else None
    return {
        "start_equity": first,
        "end_equity": last,
        "total_return": None if not first or last is None else last / first - 1.0,
        "max_drawdown": float(summary["max_drawdown"]),
        "max_drawdown_peak": _iso(summary.get("max_drawdown_peak_ts_ms")),
        "max_drawdown_trough": _iso(summary.get("max_drawdown_trough_ts_ms")),
        "longest_drawdown_days": int(summary["longest_drawdown_ms"]) / MS_PER_DAY,
        "sharpe_daily_annualized": sharpe,
        "sortino_daily_annualized": sortino,
        "annualization": "sqrt(365) on daily UTC close-of-day equity returns",
        "daily_points": int(eq.size),
    }


def _iso(ms: object) -> str | None:
    if not isinstance(ms, int):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def control_comparison(
    strategy_r: Sequence[float], control_r: Sequence[float], alpha_adj: float
) -> dict[str, Any]:
    n1, n2 = len(strategy_r), len(control_r)
    out: dict[str, Any] = {
        "n": n2,
        "win_rate": None if n2 == 0 else sum(1 for x in control_r if x > 0) / n2,
        "expectancy_r": _mean(control_r),
        "edge_r": None,
        "edge_lcb_r": None,
        "alpha_adj": alpha_adj,
        "beats_control": None,
    }
    if n1 >= 2 and n2 >= 2:
        diff = float(np.mean(strategy_r) - np.mean(control_r))
        se = math.sqrt(
            float(np.var(strategy_r, ddof=1)) / n1 + float(np.var(control_r, ddof=1)) / n2
        )
        lcb = diff - norm_ppf(1.0 - alpha_adj) * se
        out.update(edge_r=diff, edge_lcb_r=lcb, beats_control=lcb > 0.0)
    return out


# ---------------------------------------------------------------- grading
@dataclass
class GradeInputs:
    segment: str
    prereg_id: str | None
    integrity_problems: list[str]
    cert: Certification | None
    n_trades: int
    flagged_share_cap: float
    ambiguous_share: float | None
    spans_gap_share: float | None
    max_loss_measured: bool
    stress: Mapping[str, Certification | None]
    stress_means: Mapping[str, float | None]
    beats_control: bool | None
    validation_passed: bool | None = None  # holdout runs: a passing validation run exists


def grade(g: GradeInputs) -> tuple[str, list[str]]:
    caps: list[tuple[str, str]] = []
    if g.integrity_problems:
        return "X", [f"integrity: {p}" for p in g.integrity_problems]
    c = g.cert
    if c is None:
        return "C", [f"insufficient: {g.n_trades} trade(s); certification needs at least 2"]
    if c.max_loss_exceeded:
        return "D", [f"rejected: realized loss beyond max_loss_r = {c.max_loss_r:.4f}R"]
    enough = c.insufficient_n is False
    if enough and (c.margin_not_positive is True or c.expectancy_not_positive is True):
        failed = []
        if c.margin_not_positive:
            failed.append(f"certified margin {c.margin_lcb} <= 0")
        if c.expectancy_not_positive:
            failed.append(f"expectancy lower bound {c.expectancy_lcb:.6f} <= 0")
        return "D", ["rejected: " + "; ".join(failed)]
    if not c.certified:
        caps.append(("C", "insufficient: not certified (" + "; ".join(c.reasons) + ")"))
    cap = g.flagged_share_cap
    for label, share in (("ambiguous_bar", g.ambiguous_share), ("spans_gap", g.spans_gap_share)):
        if share is not None and share > cap:
            caps.append(("C", f"{label} share {share:.3f} > cap {cap:.3f}"))
    if not g.max_loss_measured:
        caps.append(("C", "max_loss_r not measured on development data (fallback used)"))
    if g.segment in ("development", "mixed", "outside"):
        caps.append(("C", f"{g.segment} segment: exploration evidence only"))
    if g.beats_control is not True:
        caps.append(("C", "no demonstrated entry edge over the random-entry control"))
    s15, s2 = g.stress.get("x1.5"), g.stress_means.get("x2")
    if s15 is None or not s15.certified:
        caps.append(("B", "cost stress x1.5 not certified"))
    if s2 is None or s2 <= 0:
        caps.append(("B", "cost stress x2 mean R not positive"))
    if g.segment == "validation":
        caps.append(("B", "validation passed; holdout pending"))
    elif g.segment == "holdout" and g.prereg_id is None:
        caps.append(("B", "holdout run without a preregistration"))
    if g.segment == "holdout" and g.validation_passed is not True:
        caps.append(("B", "no passing (grade B, certified) validation run of this configuration"))
    best = "A"
    reasons = []
    for level, why in caps:
        if GRADE_ORDER[level] < GRADE_ORDER[best]:
            best = level
        reasons.append(f"cap {level}: {why}")
    if best == "A":
        reasons.append("certified on holdout, robust to cost stress, beats random control")
    return best, reasons
