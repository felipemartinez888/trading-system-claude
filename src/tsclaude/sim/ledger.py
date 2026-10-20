"""Double-entry ledger in the quote asset, `Decimal` quantized to 1e-8 (SIMULATION §4).

Sign convention: every leg is a signed amount, **debit positive, credit negative**; the legs of
every transaction sum to exactly zero (checked on `post`, re-checked by `reconcile`).

Accounts
--------
Balance-sheet (assets, debit balances):
- ``cash``
- ``position:<market>:<symbol>`` spot inventory at cost (long only)
- ``margin:<market>:<symbol>``   USD-M collateral locked at 1x the entry notional (long or short)

Equity / income statement (credit balances are gains):
- ``capital``       contributed starting equity
- ``realized_pnl``  gross realized P&L (before fees)
- ``fees``          fees paid (debit balance)
- ``funding``       funding cash flows (interface only; posted by the caller at funding times)

Because the ledger balances, ``sum(assets) == -(capital + realized_pnl + fees + funding)``: book
equity at cost is reconstructible from entries alone. Mark-to-market equity adds the unrealized
P&L of open positions, which is a valuation, not a cash flow, and therefore not posted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from tsclaude.contracts.market import Market
from tsclaude.sim.money import ZERO, dstr, is_q8

CASH = "cash"
CAPITAL = "capital"
REALIZED_PNL = "realized_pnl"
FEES = "fees"
FUNDING = "funding"
ASSET_PREFIXES = ("position:", "margin:")
EQUITY_ACCOUNTS = (CAPITAL, REALIZED_PNL, FEES, FUNDING)


def position_account(market: Market, symbol: str) -> str:
    return f"position:{market}:{symbol}"


def margin_account(market: Market, symbol: str) -> str:
    return f"margin:{market}:{symbol}"


def is_asset(account: str) -> bool:
    return account == CASH or account.startswith(ASSET_PREFIXES)


class LedgerError(ValueError):
    """An entry would break the ledger invariants."""


@dataclass(frozen=True, slots=True)
class LedgerTx:
    tx_id: int
    ts_ms: int
    memo: str
    legs: tuple[tuple[str, Decimal], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "tx_id": self.tx_id,
            "ts_ms": self.ts_ms,
            "memo": self.memo,
            "legs": [[account, dstr(amount)] for account, amount in self.legs],
        }


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    ok: bool
    transactions: int
    assets: Decimal  # sum of asset balances replayed from entries
    equity_from_entries: Decimal  # -(capital + realized + fees + funding) replayed from entries
    problems: tuple[str, ...]


@dataclass
class Ledger:
    _entries: list[LedgerTx] = field(default_factory=list)
    _balances: dict[str, Decimal] = field(default_factory=dict)

    def post(self, ts_ms: int, memo: str, legs: Iterable[tuple[str, Decimal]]) -> LedgerTx:
        merged: dict[str, Decimal] = {}
        for account, amount in legs:
            if not isinstance(amount, Decimal) or not amount.is_finite():
                raise LedgerError(f"{memo}: non-Decimal or non-finite amount on {account}")
            if not is_q8(amount):
                raise LedgerError(f"{memo}: amount {amount} on {account} is not quantized to 1e-8")
            if not (is_asset(account) or account in EQUITY_ACCOUNTS):
                raise LedgerError(f"{memo}: unknown account {account!r}")
            merged[account] = merged.get(account, ZERO) + amount
        total = sum(merged.values(), ZERO)
        if total != ZERO:
            raise LedgerError(f"{memo}: unbalanced transaction (sum {total})")
        kept = tuple((a, v) for a, v in merged.items() if v != ZERO)
        if not kept:
            raise LedgerError(f"{memo}: empty transaction")
        tx = LedgerTx(len(self._entries) + 1, ts_ms, memo, kept)
        self._entries.append(tx)
        for account, amount in kept:
            self._balances[account] = self._balances.get(account, ZERO) + amount
        return tx

    def balance(self, account: str) -> Decimal:
        return self._balances.get(account, ZERO)

    @property
    def entries(self) -> tuple[LedgerTx, ...]:
        return tuple(self._entries)

    @property
    def balances(self) -> Mapping[str, Decimal]:
        return dict(self._balances)

    def assets(self) -> Decimal:
        return sum((v for a, v in self._balances.items() if is_asset(a)), ZERO)

    def reconcile(self) -> ReconcileReport:
        """Replay every entry into fresh balances and check all invariants."""
        problems: list[str] = []
        replay: dict[str, Decimal] = {}
        for i, tx in enumerate(self._entries, start=1):
            if tx.tx_id != i:
                problems.append(f"tx {tx.tx_id}: out of sequence (expected {i})")
            total = ZERO
            for account, amount in tx.legs:
                if not is_q8(amount):
                    problems.append(f"tx {tx.tx_id}: {account} amount not quantized")
                total += amount
                replay[account] = replay.get(account, ZERO) + amount
            if total != ZERO:
                problems.append(f"tx {tx.tx_id}: unbalanced (sum {total})")
        nonzero_replay = {a: v for a, v in replay.items() if v != ZERO}
        nonzero_running = {a: v for a, v in self._balances.items() if v != ZERO}
        if nonzero_replay != nonzero_running:
            problems.append("running balances differ from balances replayed from entries")
        assets = sum((v for a, v in replay.items() if is_asset(a)), ZERO)
        equity = -sum((replay.get(a, ZERO) for a in EQUITY_ACCOUNTS), ZERO)
        if assets != equity:
            problems.append(f"assets {assets} != equity from entries {equity}")
        if sum(replay.values(), ZERO) != ZERO:
            problems.append("trial balance is not zero")
        return ReconcileReport(not problems, len(self._entries), assets, equity, tuple(problems))
