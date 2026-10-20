"""Simulator core: one event-driven engine for backtests, replays and paper trading (ADR-0005).

Modules: `clock` (bar sources), `costs` (fees + slippage), `orders` (order types, instrument
rounding), `fills` (deterministic 1m fill rules), `ledger` (double-entry, Decimal 1e-8),
`book` (sizing, positions, trades, equity) and `engine` (the loop and run fingerprint).
"""
