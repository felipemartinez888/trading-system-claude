"""Strategies: frozen, versioned rule sets producing WATCH / CONFIRMED proposals (base.py)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from tsclaude.strategies.base import PlanningCosts, Proposal, Strategy
from tsclaude.strategies.bracket_reference import BracketReference, BracketReferenceParams


def _bracket_reference(params: Mapping[str, Any]) -> Strategy[Any]:
    known = {f.name for f in dataclasses.fields(BracketReferenceParams)}
    unknown = sorted(set(params) - known)
    if unknown:
        raise ValueError(f"unknown bracket_reference params: {unknown}")
    return BracketReference(BracketReferenceParams(**params))


#: strategy_id -> factory(params mapping); unknown ids fail closed.
REGISTRY: dict[str, Callable[[Mapping[str, Any]], Strategy[Any]]] = {
    BracketReference.strategy_id: _bracket_reference,
}


def build_strategy(strategy_id: str, params: Mapping[str, Any] | None = None) -> Strategy[Any]:
    factory = REGISTRY.get(strategy_id)
    if factory is None:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {sorted(REGISTRY)}")
    return factory(dict(params or {}))


__all__ = [
    "REGISTRY",
    "BracketReference",
    "BracketReferenceParams",
    "PlanningCosts",
    "Proposal",
    "Strategy",
    "build_strategy",
]
