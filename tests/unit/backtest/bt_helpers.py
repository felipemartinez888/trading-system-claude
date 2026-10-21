"""Synthetic lake fixtures for backtest-runner tests (no network, no pyarrow).

Prices follow a seeded random walk on a 0.01 grid with alternating calm / volatile regimes, so the
compression-breakout reference strategy produces trades. Values are synthetic, never market data.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsclaude.contracts.market import Market
from tsclaude.research.runner import BacktestConfig, BacktestRequest, RunEnv
from tsclaude.research.trials import TrialRegistry
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks
from tsclaude.store.lake import ColumnType, LakeKind, Row
from tsclaude.store.lake_reader import LakeReader
from tsclaude.store.lake_writer import LakeWriter
from tsclaude.strategies import BracketReference, BracketReferenceParams

MIN = 60_000


def ms(y: int, m: int, d: int = 1, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=UTC).timestamp() * 1000)


class JsonLinesCodec:
    """Test-only lake codec: one JSON object per line; floats round-trip exactly."""

    extension = "jsonl"

    def write(
        self, path: Path, schema: Sequence[tuple[str, ColumnType]], rows: Sequence[Row]
    ) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({n: r[n] for n, _ in schema}) + "\n")

    def read(self, path: Path, schema: Sequence[tuple[str, ColumnType]]) -> list[Row]:
        with path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def synthetic_rows(
    start: int,
    end: int,
    *,
    seed: int = 1,
    price: float = 100.0,
    skip: Sequence[int] = (),
    jumps: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = []
    t = start
    p = price
    while t < end:
        regime = (t // (6 * 3_600_000)) % 2  # 6h calm / 6h volatile
        sigma = 0.0004 if regime == 0 else 0.0025
        drift = 0.00012 if regime == 1 else 0.0
        o = round(p, 2)
        if jumps and t in jumps:
            o = round(o * jumps[t], 2)
        c = round(max(1.0, o * math.exp(rng.gauss(drift, sigma))), 2)
        h = round(max(o, c) * (1 + abs(rng.gauss(0, sigma / 2))), 2)
        lo = round(min(o, c) * (1 - abs(rng.gauss(0, sigma / 2))), 2)
        vol = round(rng.uniform(1, 3) * (4 if regime == 1 else 1), 4)
        p = c
        if t not in skip:
            rows.append(
                {
                    "open_time_ms": t,
                    "close_time_ms": t + MIN - 1,
                    "open": o,
                    "high": max(h, o, c),
                    "low": min(lo, o, c),
                    "close": c,
                    "volume": vol,
                    "quote_volume": round(vol * c, 4),
                    "trades": 10,
                    "taker_buy_volume": round(vol / 2, 4),
                    "taker_buy_quote_volume": round(vol * c / 2, 4),
                    "source": "archive",
                    "ingested_at_ms": t + MIN,
                }
            )
        t += MIN
    return rows


def write_lake(data_dir: Path, datasets: Sequence[tuple[Market, str, list[dict[str, Any]]]]) -> list[str]:
    writer = LakeWriter(data_dir, JsonLinesCodec())
    ids = []
    for market, symbol, rows in datasets:
        staging = writer.begin()
        staging.write_parts(market, LakeKind.BARS_1M, symbol, rows)
        res = staging.commit(
            {"market": str(market), "kind": str(LakeKind.BARS_1M), "symbol": symbol}, None
        )
        assert res is not None
        ids.append(res[0])
    return ids


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        self.t += 1000
        return self.t


@dataclass
class Fixture:
    env: RunEnv
    cfg: BacktestConfig
    data_dir: Path


def make_env(data_dir: Path, guard: HoldoutGuard | None = None) -> RunEnv:
    reader = LakeReader.open(data_dir, guard or HoldoutGuard(SealedUnlocks()), JsonLinesCodec())
    return RunEnv(
        reader=reader,
        trials=TrialRegistry(data_dir / "state.db"),
        runs_dir=data_dir / "runs",
        now_ms=Clock(ms(2026, 10, 21)),
        code_version=lambda: {"commit": "test", "dirty": False},
    )


TEST_CONFIG = BacktestConfig.from_mapping(
    {
        "n_resamples": 2000,
        "instruments": {
            "spot": {"default": {"tick_size": "0.01", "lot_size": "0.0001", "min_notional": "5"}}
        },
    }
)

FAST_PARAMS = BracketReferenceParams(
    interval="15m",
    atr_n=5,
    rank_m=10,
    rank_max=0.6,
    range_n=6,
    vol_n=6,
    vol_z_min=0.0,
    ema_span=5,
    rv_n=5,
    stop_atr=1.5,
    tp_r=1.0,
    max_cost_r=0.5,
)


def fast_strategy(**kw: Any) -> BracketReference:
    return BracketReference(dataclasses.replace(FAST_PARAMS, **kw))


def request(
    from_ms: int, to_ms: int, *, seed: int = 0, symbols: Sequence[str] = ("BTCUSDT",), **kw: Any
) -> BacktestRequest:
    strategy = kw.pop("strategy", None) or fast_strategy()
    return BacktestRequest(
        strategy=strategy,
        symbols=[(Market.SPOT, s) for s in symbols],
        from_ms=from_ms,
        to_ms=to_ms,
        seed=seed,
        **kw,
    )
