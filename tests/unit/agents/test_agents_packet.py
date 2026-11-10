"""Evidence packet: allowlist, masking (symbol, dates, absolute prices), no look-ahead."""

from __future__ import annotations

import dataclasses
import re

import pytest
from agent_helpers import BAR_MS, DECISION_MS, PACKET_CFG, REF, make_bars, make_inputs, make_signal

from tsclaude.agents.packet import PacketError, build_packet
from tsclaude.contracts.signals import Side


def _text() -> str:
    return build_packet(make_inputs(), PACKET_CFG).to_json()


def test_packet_masks_symbol_dates_and_absolute_prices() -> None:
    text = _text()
    low = text.lower()
    assert "solusdt" not in low
    assert "sol" not in low
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
    assert str(DECISION_MS) not in text
    assert str(DECISION_MS // 1000) not in text
    signal = make_signal()
    for price in (REF, signal.stop_price, signal.take_profit_price, REF - 0.5):
        assert f"{price}" not in text
        assert f"{price:.2f}" not in text
    for bar in make_bars():
        for v in (bar.open, bar.high, bar.low, bar.close):
            assert f"{v:.2f}" not in text
    # free-text signal fields never enter the packet
    assert "broke out" not in text
    assert "below close" not in text
    assert "time exit" not in text


def test_packet_is_relative_and_cites_by_field_id() -> None:
    packet = build_packet(make_inputs(), PACKET_CFG)
    f = packet.fields
    assert f["signal.side"] == "long"
    assert f["geometry.stop_atr"] == round(3.1 / 2.07, 4)
    assert f["geometry.tp_atr"] == round(6.2 / 2.07, 4)
    assert f["bars.ohlc_atr"][-1][3] == 0.0  # decision-bar close == reference price
    assert f["bars.count"] == PACKET_CFG.max_bars == len(f["bars.ohlc_atr"])
    assert len(f["bars.volume_rel"]) == PACKET_CFG.max_bars
    assert f["feat.regime"] == "range"
    assert f["health.streams_healthy"] is True


def test_short_geometry_is_positive_distance() -> None:
    packet = build_packet(make_inputs(make_signal(side=Side.SHORT)), PACKET_CFG)
    assert packet.fields["geometry.stop_atr"] > 0
    assert packet.fields["geometry.tp_atr"] > 0


def test_unlisted_and_unsafe_values_are_dropped_by_name_only() -> None:
    packet = build_packet(
        make_inputs(features={"atr_pct_rank": 0.13, "rsi": 61.0, "regime": "Trend-Up"}),
        PACKET_CFG,
    )
    assert "feat.rsi" not in packet.fields
    assert "feat.rsi" in packet.masked
    assert "feat.regime" not in packet.fields
    assert "feat.regime" in packet.masked
    # a condition whose values are not allowlisted keeps only `satisfied`
    cond = packet.fields["cond.close_above_range_high"]
    assert cond == {"satisfied": True}
    assert "cond.close_above_range_high.values" in packet.masked
    assert packet.fields["cond.atr_pct_rank"]["observed"] == 0.13


def test_timestamp_like_feature_values_are_dropped() -> None:
    packet = build_packet(make_inputs(features={"atr_pct_rank": float(DECISION_MS)}), PACKET_CFG)
    assert "feat.atr_pct_rank" not in packet.fields


def test_allowlisted_value_equal_to_a_price_refuses_the_packet() -> None:
    with pytest.raises(PacketError, match="price"):
        build_packet(make_inputs(features={"volume_ratio": REF}), PACKET_CFG)
    with pytest.raises(PacketError, match="price"):
        build_packet(
            make_inputs(risk_state={"open_positions": make_signal().stop_price}), PACKET_CFG
        )


def test_symbol_smuggled_through_a_label_refuses_the_packet() -> None:
    with pytest.raises(PacketError, match="symbol"):
        build_packet(make_inputs(features={"regime": "solusdt"}), PACKET_CFG)


def test_look_ahead_bar_is_refused() -> None:
    bars = [*make_bars(), dataclasses.replace(make_bars()[-1], close_time_ms=DECISION_MS + BAR_MS)]
    with pytest.raises(PacketError, match="look-ahead"):
        build_packet(make_inputs(bars=bars), PACKET_CFG)


@pytest.mark.parametrize(
    "overrides",
    [
        {"atr": 0.0},
        {"atr": float("nan")},
        {"bars": []},
        {"bars": [*make_bars()[:3], make_bars()[1]]},  # not increasing
        {"spread_bps": float("inf")},
    ],
)
def test_bad_inputs_refuse_the_packet(overrides: dict[str, object]) -> None:
    with pytest.raises(PacketError):
        build_packet(make_inputs(**overrides), PACKET_CFG)


def test_hash_is_deterministic_and_content_addressed() -> None:
    a = build_packet(make_inputs(), PACKET_CFG)
    b = build_packet(make_inputs(), PACKET_CFG)
    c = build_packet(make_inputs(spread_bps=0.9), PACKET_CFG)
    assert a.packet_hash == b.packet_hash != c.packet_hash
    assert len(a.packet_hash) == 64
