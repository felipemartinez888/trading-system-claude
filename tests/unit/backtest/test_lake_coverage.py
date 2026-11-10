"""`LakeReader.first_bar_ms`: coverage metadata from the part index (walk-forward anchor, WF-2)."""

from __future__ import annotations

from pathlib import Path

from bt_helpers import make_env, ms, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.research.walkforward import data_start_ms


def test_first_bar_comes_from_the_index_and_respects_dataset_pins(tmp_path: Path) -> None:
    early, late = ms(2023, 11, 15, 7, 3), ms(2024, 1, 2)
    ids = write_lake(
        tmp_path,
        [
            (Market.SPOT, "BTCUSDT", synthetic_rows(late, ms(2024, 2, 1))),
            (Market.SPOT, "BTCUSDT", synthetic_rows(early, ms(2023, 12, 1))),
            (Market.SPOT, "ETHUSDT", synthetic_rows(late, ms(2024, 1, 3))),
        ],
    )
    reader = make_env(tmp_path).reader
    assert reader.first_bar_ms(Market.SPOT, "BTCUSDT") == early
    assert reader.first_bar_ms(Market.SPOT, "BTCUSDT", dataset_ids=[ids[0]]) == late
    assert reader.first_bar_ms(Market.SPOT, "BTCUSDT", dataset_ids=[ids[2]]) is None
    assert reader.first_bar_ms(Market.USDM, "BTCUSDT") is None
    # every symbol needs its warm-up: the latest first bar decides, and a symbol without data
    # gives no start at all
    btc, eth = (Market.SPOT, "BTCUSDT"), (Market.SPOT, "ETHUSDT")
    assert data_start_ms(reader, [btc]) == early
    assert data_start_ms(reader, [btc, eth]) == late
    assert data_start_ms(reader, [btc, (Market.SPOT, "XRPUSDT")]) is None
