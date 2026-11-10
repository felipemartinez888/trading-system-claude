"""Strict agent output schemas and validation against the evidence packet.

An output is valid only if all of the following hold (otherwise `OutputInvalid`, which the gate
maps to ``AGENT_OUTPUT_INVALID``: treated as *no review*, never as an approval):

1. the reply is exactly one JSON object (no prose, no code fences, no duplicate keys);
2. it matches the role's schema with **no unknown keys** at any level. The schemas have no field
   for a price, size, stop, take-profit or limit, so an agent cannot express one (ADR-0007);
3. every ``evidence_refs`` entry names a packet field (optionally ``[i]`` into a list field);
4. every number in the free text exists in the packet (SIGNALS_AND_AGENTS §3).
"""

from __future__ import annotations

import json
import math
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from .packet import EvidencePacket

_NUMBER_RE = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
# field ids quoted in free text (e.g. "bars.ohlc_atr[-1]") are removed before the number scan
_INLINE_REF_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?:\[-?\d+\])?")
_REF_RE = re.compile(r"^([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?:\[(-?\d+)\])?$")

Ref = Annotated[str, StringConstraints(min_length=3, max_length=64)]
Concern = Annotated[str, StringConstraints(min_length=1, max_length=240)]
Summary = Annotated[str, StringConstraints(max_length=400)]


class OutputInvalid(ValueError):
    """Agent output rejected; the reason never contains the raw reply (it is stored separately)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Dimension(_Strict):
    score: StrictInt = Field(ge=-2, le=2)  # no bool, float or string coercion
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=8)
    concerns: tuple[Concern, ...] = Field(default=(), max_length=4)


class Dimensions(_Strict):
    regime: Dimension
    technical: Dimension
    volatility_liquidity: Dimension
    risk: Dimension

    def all(self) -> tuple[Dimension, ...]:
        return (self.regime, self.technical, self.volatility_liquidity, self.risk)


class Stance(StrEnum):
    SUPPORT = "support"
    NEUTRAL = "neutral"
    OPPOSE = "oppose"


class AnalystOutput(_Strict):
    dimensions: Dimensions
    stance: Literal["support", "neutral", "oppose"]
    summary: Summary

    @property
    def total_score(self) -> int:
        return sum(d.score for d in self.dimensions.all())

    def refs(self) -> list[str]:
        return [r for d in self.dimensions.all() for r in d.evidence_refs]

    def texts(self) -> list[str]:
        return [self.summary, *(c for d in self.dimensions.all() for c in d.concerns)]


class Severity(StrEnum):
    MINOR = "minor"
    MATERIAL = "material"
    FATAL = "fatal"


class Objection(_Strict):
    severity: Literal["minor", "material", "fatal"]
    claim: Concern
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=8)


class SkepticOutput(_Strict):
    objections: tuple[Objection, ...] = Field(max_length=6)
    summary: Summary

    def refs(self) -> list[str]:
        return [r for o in self.objections for r in o.evidence_refs]

    def texts(self) -> list[str]:
        return [self.summary, *(o.claim for o in self.objections)]

    def count(self, severity: Severity) -> int:
        return sum(1 for o in self.objections if o.severity == severity.value)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise OutputInvalid("duplicate key in JSON object")
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise OutputInvalid(f"non-finite number {name} in JSON")


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(
            text.strip(), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise OutputInvalid(f"not valid JSON (line {exc.lineno} col {exc.colno})") from None
    if not isinstance(obj, dict):
        raise OutputInvalid("reply is not a JSON object")
    return obj


def _check_refs(refs: list[str], packet: EvidencePacket) -> None:
    for ref in refs:
        m = _REF_RE.fullmatch(ref)
        if m is None or m.group(1) not in packet.fields:
            raise OutputInvalid("evidence_refs cites a field that is not in the packet")
        if m.group(2) is not None:
            value = packet.fields[m.group(1)]
            index = int(m.group(2))
            if not isinstance(value, list) or not -len(value) <= index < len(value):
                raise OutputInvalid("evidence_refs index out of range or not a list field")


def _number_in_packet(token: str, numbers: frozenset[float]) -> bool:
    x = float(token)
    decimals = len(token.split(".", 1)[1]) if "." in token else 0
    for n in numbers:
        if math.isclose(x, n, rel_tol=1e-9, abs_tol=1e-12):
            return True
        # a packet number quoted at lower precision ("1.23" for 1.2345) is still that number
        if decimals and math.isclose(round(n, decimals), x, abs_tol=10 ** -(decimals + 3)):
            return True
    return False


def _check_numbers(texts: list[str], packet: EvidencePacket) -> None:
    numbers = packet.numbers
    for text in texts:
        for token in _NUMBER_RE.findall(_INLINE_REF_RE.sub(" ", text)):
            if not _number_in_packet(token, numbers):
                raise OutputInvalid("free text contains a number that is not in the packet")


def validate_analyst(text: str, packet: EvidencePacket) -> AnalystOutput:
    try:
        out = AnalystOutput.model_validate(parse_json_object(text))
    except ValidationError as exc:
        raise OutputInvalid(f"analyst schema: {exc.error_count()} error(s)") from None
    _check_refs(out.refs(), packet)
    _check_numbers(out.texts(), packet)
    return out


def validate_skeptic(text: str, packet: EvidencePacket) -> SkepticOutput:
    try:
        out = SkepticOutput.model_validate(parse_json_object(text))
    except ValidationError as exc:
        raise OutputInvalid(f"skeptic schema: {exc.error_count()} error(s)") from None
    _check_refs(out.refs(), packet)
    _check_numbers(out.texts(), packet)
    return out
