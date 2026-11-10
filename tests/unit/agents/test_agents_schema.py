"""Strict output validation: JSON only, no unknown keys, refs must exist, no invented numbers."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from agent_helpers import PACKET_CFG, analyst_payload, make_inputs, skeptic_payload

from tsclaude.agents.packet import EvidencePacket, build_packet
from tsclaude.agents.schema import OutputInvalid, validate_analyst, validate_skeptic


@pytest.fixture
def packet() -> EvidencePacket:
    return build_packet(make_inputs(), PACKET_CFG)


def test_valid_outputs_parse(packet: EvidencePacket) -> None:
    a = validate_analyst(json.dumps(analyst_payload()), packet)
    assert a.total_score == 3
    assert a.stance == "support"
    s = validate_skeptic(json.dumps(skeptic_payload(["minor", "material"])), packet)
    assert len(s.objections) == 2


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Sure! {",
        "```json\n" + json.dumps(analyst_payload()) + "\n```",
        "Here you go: " + json.dumps(analyst_payload()),
        json.dumps([analyst_payload()]),
        '{"stance": "support", "stance": "oppose"}',
        '{"x": NaN}',
    ],
)
def test_non_json_object_replies_are_invalid(packet: EvidencePacket, text: str) -> None:
    with pytest.raises(OutputInvalid):
        validate_analyst(text, packet)


def _mutate(payload: dict[str, Any], path: list[str], value: Any) -> dict[str, Any]:
    p = copy.deepcopy(payload)
    target = p
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = value
    return p


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["stop_price"], 1.0),  # no field can express a price
        (["position_size"], 0.5),
        (["dimensions", "risk", "take_profit"], 2.0),
        (["dimensions", "fifth"], {"score": 1, "evidence_refs": ["feat.regime"]}),
        (["dimensions", "risk", "score"], 3),
        (["dimensions", "risk", "score"], "1"),
        (["dimensions", "risk", "score"], True),
        (["dimensions", "risk", "score"], 1.0),
        (["dimensions", "risk", "evidence_refs"], []),
        (["stance"], "strong_buy"),
        (["summary"], "x" * 401),
    ],
)
def test_schema_violations_are_invalid(packet: EvidencePacket, path: list[str], value: Any) -> None:
    with pytest.raises(OutputInvalid):
        validate_analyst(json.dumps(_mutate(analyst_payload(), path, value)), packet)


def test_skeptic_cannot_carry_extra_fields(packet: EvidencePacket) -> None:
    payload = skeptic_payload()
    payload["objections"][0]["new_stop_atr"] = 2.0
    with pytest.raises(OutputInvalid):
        validate_skeptic(json.dumps(payload), packet)
    payload = skeptic_payload()
    payload["objections"][0]["severity"] = "catastrophic"
    with pytest.raises(OutputInvalid):
        validate_skeptic(json.dumps(payload), packet)


@pytest.mark.parametrize(
    "ref",
    [
        "feat.rsi",
        "feat.nope",
        "bars.ohlc_atr[12]",
        "bars.ohlc_atr[-13]",
        "costs.rr_net[0]",
        "nofield",
    ],
)
def test_refs_must_name_packet_fields(packet: EvidencePacket, ref: str) -> None:
    payload = _mutate(analyst_payload(), ["dimensions", "risk", "evidence_refs"], [ref])
    with pytest.raises(OutputInvalid, match="evidence_refs"):
        validate_analyst(json.dumps(payload), packet)


def test_list_index_refs_within_range_are_valid(packet: EvidencePacket) -> None:
    refs = ["bars.ohlc_atr[0]", "bars.ohlc_atr[-12]", "bars.volume_rel[11]"]
    payload = _mutate(analyst_payload(), ["dimensions", "risk", "evidence_refs"], refs)
    validate_analyst(json.dumps(payload), packet)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Spread of 0.8 bps is tight.", True),  # in the packet
        ("Net reward to risk 1.83 clears the bar.", True),
        ("Stop is 1.5 ATR away.", True),  # 1.4976 in the packet, quoted at 1 dp
        ("Stop at 184.33 looks fine.", False),  # an absolute price: never in the packet
        ("Win rate above 55% expected.", False),  # invented
        ("The last bar bars.ohlc_atr[-1] closed at the reference.", True),  # inline ref ignored
    ],
)
def test_numbers_in_free_text_must_exist_in_packet(
    packet: EvidencePacket, text: str, ok: bool
) -> None:
    payload = _mutate(analyst_payload(), ["dimensions", "risk", "concerns"], [text])
    if ok:
        validate_analyst(json.dumps(payload), packet)
    else:
        with pytest.raises(OutputInvalid, match="number"):
            validate_analyst(json.dumps(payload), packet)


def test_lower_precision_quote_of_a_packet_number_is_accepted(packet: EvidencePacket) -> None:
    assert packet.fields["geometry.stop_atr"] == 1.4976
    for text in ("Stop about 1.50 ATR.", "Stop about 1.498 ATR.", "Stop 1.4976 ATR."):
        validate_analyst(json.dumps(_mutate(analyst_payload(), ["summary"], text)), packet)
    # 1.49 is not 1.4976 at any precision
    with pytest.raises(OutputInvalid, match="number"):
        validate_analyst(
            json.dumps(_mutate(analyst_payload(), ["summary"], "Stop 1.49 ATR.")), packet
        )


_FORBIDDEN = ("price", "size", "qty", "stop", "take_profit", "leverage", "entry", "risk_limit")


def test_property_no_payload_with_an_extra_key_anywhere_validates(packet: EvidencePacket) -> None:
    """Advise-only at the schema level: 500 random placements of a forbidden key all fail."""
    rng = random.Random(20261110)
    for _ in range(500):
        payload = analyst_payload(
            tuple(rng.randint(-2, 2) for _ in range(4)),  # type: ignore[arg-type]
            rng.choice(["support", "neutral", "oppose"]),
        )
        where = rng.choice(
            [[], ["dimensions"], *(["dimensions", d] for d in payload["dimensions"])]
        )
        target = payload
        for k in where:
            target = target[k]
        target[rng.choice(_FORBIDDEN)] = rng.choice([1.0, "0.5", {"value": 2}, [3]])
        with pytest.raises(OutputInvalid):
            validate_analyst(json.dumps(payload), packet)
        sk = skeptic_payload(rng.choice([[], ["minor"], ["fatal", "material"]]))
        tgt = sk["objections"][0] if sk["objections"] and rng.random() < 0.5 else sk
        tgt[rng.choice(_FORBIDDEN)] = 1.0
        with pytest.raises(OutputInvalid):
            validate_skeptic(json.dumps(sk), packet)
