"""Strategies: frozen, versioned rule sets producing WATCH / CONFIRMED proposals (base.py)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from tsclaude.strategies.base import PlanningCosts, Proposal, Strategy
from tsclaude.strategies.bracket_reference import BracketReference, BracketReferenceParams
from tsclaude.strategies.h1_compression_breakout import (
    H1CompressionBreakout,
    H1CompressionBreakoutParams,
)


def _bracket_reference(params: Mapping[str, Any]) -> Strategy[Any]:
    known = {f.name for f in dataclasses.fields(BracketReferenceParams)}
    unknown = sorted(set(params) - known)
    if unknown:
        raise ValueError(f"unknown bracket_reference params: {unknown}")
    return BracketReference(BracketReferenceParams(**params))


def _h1_compression_breakout(params: Mapping[str, Any]) -> Strategy[Any]:
    known = {f.name for f in dataclasses.fields(H1CompressionBreakoutParams)}
    unknown = sorted(set(params) - known)
    if unknown:
        raise ValueError(f"unknown h1_compression_breakout params: {unknown}")
    return H1CompressionBreakout(H1CompressionBreakoutParams(**params))


#: strategy_id -> factory(params mapping); unknown ids fail closed.
REGISTRY: dict[str, Callable[[Mapping[str, Any]], Strategy[Any]]] = {
    BracketReference.strategy_id: _bracket_reference,
}


#: Strategies awaiting an owner-approved freeze and promotion to `REGISTRY`. Nothing that runs a
#: backtest or records a trial reads this mapping (`backtest run`, `walkforward` and the runner use
#: `build_strategy`); only the P&L-free feasibility count (``backtest feasibility --draft``) and
#: preregistration validation (which lists the draft status as an open item) build from it.
DRAFT_REGISTRY: dict[str, Callable[[Mapping[str, Any]], Strategy[Any]]] = {
    H1CompressionBreakout.strategy_id: _h1_compression_breakout,
}


def build_strategy(strategy_id: str, params: Mapping[str, Any] | None = None) -> Strategy[Any]:
    factory = REGISTRY.get(strategy_id)
    if factory is None:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {sorted(REGISTRY)}")
    return factory(dict(params or {}))


def build_draft_strategy(
    strategy_id: str, params: Mapping[str, Any] | None = None
) -> Strategy[Any]:
    """Build a DRAFT strategy (P&L-free feasibility counts and validation only; never a run)."""
    factory = DRAFT_REGISTRY.get(strategy_id)
    if factory is None:
        raise ValueError(
            f"unknown draft strategy {strategy_id!r}; drafts: {sorted(DRAFT_REGISTRY)}"
        )
    return factory(dict(params or {}))


__all__ = [
    "DRAFT_REGISTRY",
    "REGISTRY",
    "BracketReference",
    "BracketReferenceParams",
    "H1CompressionBreakout",
    "H1CompressionBreakoutParams",
    "PlanningCosts",
    "Proposal",
    "Strategy",
    "build_draft_strategy",
    "build_strategy",
]
