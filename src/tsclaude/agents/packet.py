"""Evidence packet builder (SIGNALS_AND_AGENTS §3; REQ-AGT-02).

A packet contains facts only, as a flat mapping ``field_id -> value`` that agents must cite:

- ``signal.*``   side, timeframe, market type, entry-rule kind, strategy id;
- ``geometry.*`` stop / take-profit / entry distance from the reference price in ATR units;
- ``costs.*``    R:R gross/net, modeled cost in bps, break-even win rate;
- ``market.*``   spread and funding in bps (optional);
- ``feat.*``, ``health.*``, ``risk.*`` allowlisted, dimensionless values only (config);
- ``cond.*``     entry conditions; numeric values only for allowlisted names;
- ``bars.*``     the last N bars, oldest first, the decision bar last: OHLC relative to the
                 reference price in ATR units, volume relative to the window mean.

Masking: the symbol, every calendar date/timestamp and every absolute price level are absent.
The finished packet is then *audited* (`audit_masking`); a packet that fails the audit is never
sent (`PacketError`, which the gate maps to ``AI_UNAVAILABLE``). No look-ahead: a bar whose close
time is after the decision time is refused.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.signals import Side, SignalRecord

from .config import PacketConfig

PACKET_SCHEMA_VERSION = "packet-v1"
_DECIMALS = 4
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")  # short lowercase labels only
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_DIGIT_RUN_RE = re.compile(r"\d{4,}")  # labels with a year/date-like digit run are dropped
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
_EPOCH_LIKE = 1e11  # any number this large could be an epoch-ms timestamp


class PacketError(ValueError):
    """The packet cannot be built or failed the masking audit; never sent to an agent."""


@dataclass(frozen=True)
class BarInput:
    """One strategy bar, as known at the decision time (close_time_ms <= decision time)."""

    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PacketInputs:
    signal: SignalRecord
    bars: Sequence[BarInput]
    atr: float  # ATR at the decision bar, in price units (only used to normalize)
    features: Mapping[str, float | int | str | bool] = field(default_factory=dict)
    data_health: Mapping[str, float | int | str | bool] = field(default_factory=dict)
    risk_state: Mapping[str, float | int | str | bool] = field(default_factory=dict)
    spread_bps: float | None = None
    funding_bps: float | None = None


@dataclass(frozen=True)
class EvidencePacket:
    fields: dict[str, Any]
    masked: tuple[str, ...]  # names (never values) that were dropped
    packet_hash: str

    def to_json(self) -> str:
        return canonical_json(
            {"schema": PACKET_SCHEMA_VERSION, "fields": self.fields, "masked": list(self.masked)}
        )

    @property
    def numbers(self) -> frozenset[float]:
        """Every number appearing in the packet (used to reject invented numbers)."""
        return frozenset(float(m) for m in _NUMBER_RE.findall(self.to_json()))


def _r(x: float) -> float:
    v = round(x, _DECIMALS)
    return 0.0 if v == 0 else v  # no "-0.0"


def _finite(x: float, what: str) -> float:
    if not math.isfinite(x):
        raise PacketError(f"{what} is not finite")
    return x


def _scalar(value: object) -> float | int | str | bool | None:
    """A dimensionless scalar, or None if it may not be included."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        f = float(value)
        if not math.isfinite(f) or abs(f) >= _EPOCH_LIKE:
            return None
        return value if isinstance(value, int) else _r(f)
    if isinstance(value, str) and _LABEL_RE.fullmatch(value) and not _DIGIT_RUN_RE.search(value):
        return value
    return None


def _check_not_price(value: object, prices: Sequence[float], key: str) -> None:
    """An allowlisted value equal to a raw price of the signal or its bars is a leak: refuse the
    whole packet (an allowlist entry is misconfigured), compared before any rounding."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return
    v = float(value)
    if any(math.isclose(v, p, rel_tol=1e-9) for p in prices):
        raise PacketError(f"absolute price level leaked through {key}")


def _section(
    prefix: str,
    values: Mapping[str, object],
    allowed: frozenset[str],
    out: dict[str, Any],
    masked: list[str],
    prices: Sequence[float],
) -> None:
    for name in sorted(values):
        key = f"{prefix}.{name}"
        if name not in allowed or not _NAME_RE.fullmatch(name):
            masked.append(key if _NAME_RE.fullmatch(name) else f"{prefix}.<invalid-name>")
            continue
        _check_not_price(values[name], prices, key)
        v = _scalar(values[name])
        if v is None:
            masked.append(key)
            continue
        out[key] = v


def build_packet(inputs: PacketInputs, config: PacketConfig) -> EvidencePacket:
    sig = inputs.signal
    atr = _finite(inputs.atr, "atr")
    if atr <= 0:
        raise PacketError("atr must be positive")
    ref = _finite(sig.ref_price, "ref_price")
    bars = list(inputs.bars)
    if not bars:
        raise PacketError("no bars")
    for b in bars:
        if b.close_time_ms > sig.decision_time_ms:
            raise PacketError("bar after the decision time (look-ahead) refused")
    if any(b2.close_time_ms <= b1.close_time_ms for b1, b2 in pairwise(bars)):
        raise PacketError("bars are not strictly increasing in time")
    if sig.decision_time_ms - bars[-1].close_time_ms >= sig.timeframe.ms:
        raise PacketError("stale bars: the last bar is not the decision bar")
    bars = bars[-config.max_bars :]

    def rel(price: float) -> float:
        return _r((_finite(price, "price") - ref) / atr)

    long = sig.side is Side.LONG
    f: dict[str, Any] = {
        "signal.side": str(sig.side),
        "signal.timeframe": str(sig.timeframe),
        "signal.market_type": str(sig.market),
        "signal.entry_rule": str(sig.entry_rule.kind),
        "signal.strategy": sig.strategy_id if _NAME_RE.fullmatch(sig.strategy_id) else "masked",
        "geometry.stop_atr": _r(abs(ref - sig.stop_price) / atr),
        "costs.rr_gross": _r(_finite(sig.rr_gross, "rr_gross")),
        "costs.rr_net": _r(_finite(sig.rr_net, "rr_net")),
        "costs.est_cost_bps": _r(_finite(sig.est_cost_bps, "est_cost_bps")),
        "costs.break_even_win_rate": _r(sig.break_even_win_rate),
    }
    if sig.take_profit_price is not None:
        f["geometry.tp_atr"] = _r(abs(sig.take_profit_price - ref) / atr)
    if sig.entry_rule.price is not None:
        # signed in the trade direction: positive = entry beyond the reference price
        offset = (sig.entry_rule.price - ref) / atr
        f["geometry.entry_offset_atr"] = _r(offset if long else -offset)
    if inputs.spread_bps is not None:
        f["market.spread_bps"] = _r(_finite(inputs.spread_bps, "spread_bps"))
    if inputs.funding_bps is not None:
        f["market.funding_bps"] = _r(_finite(inputs.funding_bps, "funding_bps"))

    prices = [ref, sig.stop_price, *(b.open for b in bars), *(b.close for b in bars)]
    prices += [b.high for b in bars] + [b.low for b in bars]
    if sig.take_profit_price is not None:
        prices.append(sig.take_profit_price)
    if sig.entry_rule.price is not None:
        prices.append(sig.entry_rule.price)
    prices = [p for p in prices if math.isfinite(p) and p != 0]

    masked: list[str] = []
    _section("feat", inputs.features, config.allowed_features, f, masked, prices)
    _section("health", inputs.data_health, config.allowed_health, f, masked, prices)
    _section("risk", inputs.risk_state, config.allowed_risk, f, masked, prices)

    for cond in sorted(sig.entry_conditions, key=lambda c: c.name):
        if not _NAME_RE.fullmatch(cond.name):
            masked.append("cond.<invalid-name>")
            continue
        entry: dict[str, Any] = {"satisfied": cond.satisfied}
        if cond.name in config.allowed_features:
            _check_not_price(cond.threshold, prices, f"cond.{cond.name}")
            _check_not_price(cond.observed, prices, f"cond.{cond.name}")
            threshold, observed = _scalar(cond.threshold), _scalar(cond.observed)
            if threshold is not None and observed is not None:
                entry |= {"operator": cond.operator, "threshold": threshold, "observed": observed}
            else:
                masked.append(f"cond.{cond.name}.values")
        else:
            masked.append(f"cond.{cond.name}.values")
        f[f"cond.{cond.name}"] = entry

    vols = [_finite(b.volume, "volume") for b in bars]
    mean_vol = sum(vols) / len(vols)
    f["bars.count"] = len(bars)
    f["bars.ohlc_atr"] = [[rel(b.open), rel(b.high), rel(b.low), rel(b.close)] for b in bars]
    f["bars.volume_rel"] = [_r(v / mean_vol) if mean_vol > 0 else 0.0 for v in vols]

    body = {"schema": PACKET_SCHEMA_VERSION, "fields": f, "masked": sorted(set(masked))}
    text = canonical_json(body)
    audit_masking(text, sig.symbol)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return EvidencePacket(fields=f, masked=tuple(sorted(set(masked))), packet_hash=digest)


def _symbol_tokens(symbol: str) -> list[str]:
    tokens = [symbol.lower()]
    for quote in _QUOTES:
        if symbol.upper().endswith(quote) and len(symbol) - len(quote) >= 3:
            tokens.append(symbol[: -len(quote)].lower())
            break
    return tokens


def audit_masking(text: str, symbol: str) -> None:
    """Raise `PacketError` if the packet text could reveal the symbol, a date or a timestamp.

    (Absolute prices are checked on the raw allowlisted values by `_check_not_price`; bar and
    geometry values are relative by construction and are not compared, since on low-priced
    assets they would collide with a price by chance.)
    """
    low = text.lower()
    for token in _symbol_tokens(symbol):
        if token and token in low:
            raise PacketError("symbol leaked into the packet")
    if _ISO_DATE_RE.search(text):
        raise PacketError("calendar date leaked into the packet")
    for m in _NUMBER_RE.findall(text):
        if abs(float(m)) >= _EPOCH_LIKE:
            raise PacketError("timestamp-like number leaked into the packet")
