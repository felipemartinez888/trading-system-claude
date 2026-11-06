"""OD-2: restart outcomes RECOVERED / SAFE_BLOCK / FAIL (`paper_scenarios.classify_restart`).

A SAFE_BLOCK is a fail-closed stop that the oracle proves unavoidable (no store could hold the
evidence a resume needs) and safe (S1-S7). It is not a successful recovery, not replay
equivalence and not availability: the M3 availability and autonomous-recovery requirements stay
open (docs/tasks/M3-PB.md, OD-2). These tests pin the classification on known cases, show that the
oracle is independent of production's missing-data logic, and that mutants of production and of
the oracle are caught, among them the ones that guess a missing minute is empty.
"""

from __future__ import annotations

import dataclasses
import random
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from paper_helpers import BTC, ETH
from paper_scenarios import (
    EMPTY_MINUTE,
    FAIL,
    INSTRUMENT_GAP,
    RECOVERED,
    SAFE_BLOCK,
    BarStore,
    Restart,
    bar_store,
    check_restore_equivalence,
    classify_restart,
    journal_exposure,
    outcome_after_downtime,
    recovery_evidence,
    recovery_outcome,
    redeliver,
    report_outcome,
    restart_outcome,
    restore_after_crash,
    run_random_session,
    runtime_backfill,
    stability_polls,
)

import tsclaude.runtime.paper_runtime as paper_runtime
from tsclaude.contracts.market import Market
from tsclaude.runtime.paper_runtime import PaperRuntime
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperTradingService, ServiceState
from tsclaude.sim.testing import minute

M = 60_000

#: instant restarts that block on a genuinely empty minute: (seed, fail_batch) -> (durable
#: batches, empty minute index). The first two are the OD-1 counterexamples (50553820 is
#: fail_batch 135, modulo the session's 72 batches = 63); the other two came from a sweep over every
#: crash point of 20 seeds (130741648, 50553820 and 18 drawn by random.Random(20261105)):
#: 1534 RECOVERED, 4 SAFE_BLOCK, 0 FAIL
EMPTY_MINUTE_BLOCKS = {
    (130741648, 8): (8, 4),
    (50553820, 135): (63, 34),
    (138207719, 69): (69, 35),
    (454919826, 27): (27, 14),
}
#: instant restarts known to recover with strict equality (pinned property examples and seeded
#: cases)
STRICT_RECOVERABLE = [
    (13073, 124),
    (465923499, 162),
    (23511, 118),
    (0, 5),
    (1, 17),
    (2, 40),
    (3, 61),
    (4, 77),
]
#: the downtime scenario of the dedicated tests: BTC and ETH exposed, journal through minute 7,
#: first live minute 19, every downtime bar of minutes 8-18 exists
DOWNTIME = (465923499, 162, 600_000)
X = 12  # a downtime minute in the middle (not the one before the first live minute)
TAIL = 18  # the minute before the first live minute


def _downtime() -> Restart:
    r = restore_after_crash(*DOWNTIME)
    assert isinstance(r, Restart)
    return r


def _without(r: Restart, *gone: tuple[int, str]) -> BarStore:
    """The live store without some bars that existed (store data loss)."""
    truth = bar_store(r)
    lost = {(minute(i), s) for i, s in gone}

    def read(market: Market, symbol: str, start: int, end: int) -> list[Any]:
        return [
            b for b in truth(market, symbol, start, end) if (b.open_time_ms, symbol) not in lost
        ]

    return read


@pytest.fixture
def guess_gaps_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production mutant G1: after a restore, an unexplained gap before the first live bar is
    assumed to be a minute without bars, so the service resumes without any backfill."""

    def contiguous(self: PaperTradingService) -> bool:
        if not self._check_continuity:
            return True
        if self.clock.earliest_buffered() is None:
            return False
        self._check_continuity = False
        self.awaiting_missing = {}
        self.state = ServiceState.RUNNING
        return True

    monkeypatch.setattr(PaperTradingService, "_contiguous", contiguous)


@pytest.fixture
def guess_backfill_gaps_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production mutant G2: a downtime minute the backfill does not hold is assumed to be a
    minute without bars (the completeness check sees nothing missing once a backfill is given)."""
    original = PaperTradingService.missing_before

    def missing_before(
        self: PaperTradingService, first_live_ms: int, extra: Any = ()
    ) -> dict[str, list[int]]:
        return {} if extra else original(self, first_live_ms, extra)

    monkeypatch.setattr(PaperTradingService, "missing_before", missing_before)


# ---------------------------------------------------------------- known cases
@pytest.mark.parametrize(("seed", "fail_batch"), sorted(EMPTY_MINUTE_BLOCKS))
def test_an_empty_minute_at_the_crash_point_is_a_safe_block(seed: int, fail_batch: int) -> None:
    """SAFE_BLOCK, not recovery: the strict check still fails (unchanged), the service keeps
    exactly the durable prefix and the oracle proves that no store holds the empty minute."""
    durable, empty = EMPTY_MINUTE_BLOCKS[(seed, fail_batch)]
    assert check_restore_equivalence(seed, fail_batch) != []  # not equivalence
    out = restart_outcome(seed, fail_batch)
    assert (out.kind, out.reason, out.problems) == (SAFE_BLOCK, EMPTY_MINUTE, ())
    ev = out.evidence
    m = minute(empty)
    assert ev.last_minute_ms == m - M
    assert ev.closed_through_ms == m  # the minute before the empty one was closed
    assert ev.first_live_ms == m + M
    assert ev.absent == {(m, "BTCUSDT"), (m, "ETHUSDT"), (m, None)}
    r = restore_after_crash(seed, fail_batch)
    assert isinstance(r, Restart)
    assert r.durable == durable


@pytest.mark.parametrize(("seed", "fail_batch"), STRICT_RECOVERABLE)
def test_known_recoverable_cases_stay_strictly_equal(seed: int, fail_batch: int) -> None:
    assert check_restore_equivalence(seed, fail_batch) == []
    out = restart_outcome(seed, fail_batch)
    assert (out.kind, out.problems) == (RECOVERED, ())


@pytest.mark.parametrize("seed", [130741648, 50553820])
def test_only_the_empty_minute_crash_point_blocks(seed: int) -> None:
    """Every crash point of the session: one SAFE_BLOCK (the empty minute), the rest RECOVERED."""
    n = len(run_random_session(seed)[1].batches())
    outcomes = {fb: restart_outcome(seed, fb) for fb in range(n)}
    blocked = {fb for fb, o in outcomes.items() if o.kind != RECOVERED}
    point = next(fb % n for (s, fb) in EMPTY_MINUTE_BLOCKS if s == seed)
    assert blocked == {point}
    assert outcomes[point].kind == SAFE_BLOCK


# ---------------------------------------------------------------- empty minute vs feed loss
def test_a_bar_missing_only_from_the_live_feed_is_backfilled() -> None:
    """Downtime: minutes 8-18 never reached the live feed, but the store has them. Production
    backfills them (unobserved, no entry from them) and recovers."""
    r = _downtime()
    out = outcome_after_downtime(r)
    assert (out.kind, out.problems) == (RECOVERED, ())
    after = [b[0].payload for b in r.journal.batches()[r.durable :]]
    backfilled = [(p["bar"]["open_time_ms"], p["observed"]) for p in after if p["kind"] == "bar"]
    assert (minute(X), False) in backfilled
    assert out.evidence.exposed == {(Market.SPOT, "BTCUSDT"), (Market.SPOT, "ETHUSDT")}


@pytest.mark.parametrize(
    ("gone", "reason", "absent"),
    [
        ({(X, "BTCUSDT"), (X, "ETHUSDT")}, EMPTY_MINUTE, {(X, "BTCUSDT"), (X, "ETHUSDT")}),
        ({(X, "BTCUSDT")}, INSTRUMENT_GAP, {(X, "BTCUSDT")}),
        (
            {(TAIL, "BTCUSDT"), (TAIL, "ETHUSDT")},
            EMPTY_MINUTE,
            {(TAIL, "BTCUSDT"), (TAIL, "ETHUSDT"), (TAIL, None)},
        ),
    ],
)
def test_a_minute_that_never_had_the_bar_is_a_safe_block(
    gone: set[tuple[int, str]], reason: str, absent: set[tuple[int, str | None]]
) -> None:
    """The same downtime, with bars that never existed (ground truth: not in any store): the
    feed delivers exactly what it delivered above, and the service must stay blocked."""
    r = _downtime()
    r.missing = r.missing | gone
    out = outcome_after_downtime(r)
    assert (out.kind, out.reason, out.problems) == (SAFE_BLOCK, reason, ())
    assert out.evidence.absent == {(minute(i), s) for i, s in absent}
    assert r.restored.state is ServiceState.AWAITING_BACKFILL
    assert len(r.journal.batches()) == r.durable


@pytest.mark.usefixtures("guess_gaps_empty")
def test_guessing_a_feed_gap_is_empty_fails_the_feed_loss_scenario() -> None:
    """G1 resumes without a backfill: the bars missing only from the feed are never processed."""
    r = _downtime()
    out = outcome_after_downtime(r)
    assert out.kind == FAIL
    assert any("downtime bars of exposed instruments not processed" in p for p in out.problems)


@pytest.mark.usefixtures("guess_gaps_empty")
def test_guessing_the_empty_minute_is_empty_fails_even_when_lucky() -> None:
    """G1 at the OD-1 counterexample: resuming happens to reproduce the uninterrupted journal,
    but nothing persisted proves the minute empty, so it is an unsafe continuation."""
    out = restart_outcome(130741648, 8)
    assert out.kind == FAIL
    assert any("resumed without the evidence" in p for p in out.problems)


@pytest.mark.usefixtures("guess_backfill_gaps_empty")
def test_guessing_a_backfill_gap_is_empty_fails() -> None:
    """G2 has nothing to guess when the store is complete (the feed-loss scenario recovers), but
    resumes over a minute that never had its bar (unsafe) and over a bar the store lost."""
    assert outcome_after_downtime(_downtime()).kind == RECOVERED
    r = _downtime()
    r.missing = r.missing | {(X, "BTCUSDT"), (X, "ETHUSDT")}
    out = outcome_after_downtime(r)
    assert out.kind == FAIL
    assert any("resumed without the evidence" in p for p in out.problems)
    r = _downtime()
    out = outcome_after_downtime(r, _without(r, (X, "BTCUSDT")))
    assert out.kind == FAIL
    assert any("downtime bars of exposed instruments not processed" in p for p in out.problems)


# ---------------------------------------------------------------- store data loss
@pytest.mark.parametrize(
    "gone", [((X, "BTCUSDT"),), ((TAIL, "BTCUSDT"), (TAIL, "ETHUSDT"))], ids=["middle", "tail"]
)
def test_a_block_caused_by_store_data_loss_is_a_failure(gone: tuple[tuple[int, str], ...]) -> None:
    """The store lost bars that existed. Production fails closed (correct for its input), and a
    classifier that accepted any AWAITING_BACKFILL would call it safe; the oracle knows the bars
    existed, so the block is unjustified: FAIL, never SAFE_BLOCK."""
    r = _downtime()
    out = outcome_after_downtime(r, _without(r, *gone))
    assert r.restored.state is ServiceState.AWAITING_BACKFILL
    assert len(r.journal.batches()) == r.durable
    assert out.kind == FAIL
    assert out.evidence.feasible
    assert any(p.startswith("E: blocked although") for p in out.problems)


# ---------------------------------------------------------------- safety invariants
def test_journaling_after_a_block_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutant that processes the buffered live bars once it has blocked (S1, S3, S7)."""
    original = PaperTradingService._contiguous
    blocked: set[int] = set()

    def leaky(self: PaperTradingService) -> bool:
        if id(self) in blocked:
            return True
        ok = original(self)
        if not ok and self.state is ServiceState.AWAITING_BACKFILL:
            blocked.add(id(self))
        return ok

    monkeypatch.setattr(PaperTradingService, "_contiguous", leaky)
    out = restart_outcome(130741648, 8)
    assert out.kind == FAIL
    assert any(p.startswith("S1:") for p in out.problems)


def test_a_block_without_the_runtime_backfill_attempt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6: a safe block needs the runtime's backfill attempt, refused for completeness only."""
    monkeypatch.setattr(PaperRuntime, "_attempt_backfill", lambda self, svc, now: None)
    out = restart_outcome(130741648, 8)
    assert out.kind == FAIL
    assert any(p.startswith("S6:") for p in out.problems)


def test_a_block_refused_for_another_reason_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_store(market: Market, symbol: str, start: int, end: int) -> list[Any]:
        raise OSError("store unavailable")

    out = restart_outcome(130741648, 8, store=broken_store)
    assert out.kind == FAIL
    assert any(p.startswith("S6: backfill refused for another reason") for p in out.problems)


def _blocked() -> tuple[Restart, list[str], list[tuple[int, int, bool]]]:
    """The OD-1 counterexample 130741648/8, recovered as `restart_outcome` does it."""
    strict = check_restore_equivalence(130741648, 8)
    r = restore_after_crash(130741648, 8)
    assert isinstance(r, Restart)
    redeliver(r)
    runtime_backfill(r, minute(40) + 1_500)
    return r, strict, stability_polls(r)


def _tamper_prefix(r: Restart, st: list[Any]) -> list[Any]:
    ref = r.ref_journal.batches()
    r.ref_journal = MemoryJournal(_batches=[ref[1], ref[0], *ref[2:]])
    return st


def _tamper_journal(r: Restart, st: list[Any]) -> list[Any]:
    r.journal = MemoryJournal(_batches=[*r.journal.batches(), r.journal.batches()[-1]])
    return st


def _tamper_strategy(r: Restart, st: list[Any]) -> list[Any]:
    r.strategy.calls.append((minute(5), "BTCUSDT"))
    return st


def _tamper_session(r: Restart, st: list[Any]) -> list[Any]:
    r.restored.session.batches += 1
    return st


def _tamper_ledgers(r: Restart, st: list[Any]) -> list[Any]:
    r.restored.ledger_hashes = lambda: {"baseline": "forged"}  # type: ignore[method-assign]
    return st


def _tamper_awaiting(r: Restart, st: list[Any]) -> list[Any]:
    r.restored.awaiting_missing = {}
    return st


def _tamper_awaiting_range(r: Restart, st: list[Any]) -> list[Any]:
    r.restored.awaiting_missing = {"*": [minute(30)]}
    return st


def _tamper_awaiting_other(r: Restart, st: list[Any]) -> list[Any]:
    r.restored.awaiting_missing = {"*": [minute(3)]}  # names a minute, but not the empty one
    return st


def _tamper_backfill(r: Restart, st: list[Any]) -> list[Any]:
    r.backfill = None
    return st


def _tamper_backfill_reason(r: Restart, st: list[Any]) -> list[Any]:
    r.backfill = {"ok": False, "processed": 0, "problems": ["BTC: duplicate bar"]}
    return st


def _tamper_stability(r: Restart, st: list[Any]) -> list[Any]:
    return [*st, (r.durable, r.calls_at_restore, False)]


def _tamper_truth(r: Restart, st: list[Any]) -> list[Any]:
    r.missing = r.missing - {(4, "BTCUSDT"), (4, "ETHUSDT")}  # the minute had bars after all
    return st


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        (_tamper_prefix, "the durable prefix is not"),
        (_tamper_journal, "S1:"),
        (_tamper_strategy, "S2:"),
        (_tamper_session, "S3:"),
        (_tamper_ledgers, "S4:"),
        (_tamper_awaiting, "S5: blocked without"),
        (_tamper_awaiting_range, "S5: * names a minute outside"),
        (_tamper_awaiting_other, "S5: the missing minutes"),
        (_tamper_backfill, "S6: no refused backfill"),
        (_tamper_backfill_reason, "S6: backfill refused for another reason"),
        (_tamper_stability, "S7:"),
        (_tamper_truth, "E: blocked although"),
    ],
)
def test_each_safe_block_condition_is_checked(
    tamper: Callable[[Restart, list[Any]], list[Any]], expected: str
) -> None:
    """Oracle meta-test: break one condition of a real SAFE_BLOCK at a time; each is reported."""
    r, strict, st = _blocked()
    assert classify_restart(r, strict, instant=True, stability=st).kind == SAFE_BLOCK
    out = classify_restart(r, strict, instant=True, stability=tamper(r, st))
    assert out.kind == FAIL
    assert any(p.startswith(expected) for p in out.problems), out.problems


def test_resuming_on_a_later_poll_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutant that gives up waiting and resumes on a poll an hour after the block. Only the
    stability polls (S7, up to a day later) reach it; the resumed service has no evidence for
    the empty minute, so it is an unsafe continuation."""
    original = PaperTradingService.poll

    def poll(self: PaperTradingService, now_ms: int) -> int:
        if self.state is ServiceState.AWAITING_BACKFILL and now_ms > minute(40) + 3_600_000:
            self._check_continuity = False
            self.awaiting_missing = {}
            self.state = ServiceState.RUNNING
        return original(self, now_ms)

    monkeypatch.setattr(PaperTradingService, "poll", poll)
    out = restart_outcome(130741648, 8)
    assert out.kind == FAIL
    assert any(p.startswith("resumed without the evidence") for p in out.problems)


# ---------------------------------------------------------------- oracle independence
def test_the_oracle_does_not_consult_production_missing_data_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evidence is the same with the restored service removed and every production
    missing-data function sabotaged: it comes from the journal and the ground truth only."""
    for case in (DOWNTIME, (130741648, 8, 0)):
        r = restore_after_crash(*case)
        assert isinstance(r, Restart)
        live_from = None if case[2] == 0 else r.restart_ms
        before = recovery_evidence(r, live_from_ms=live_from)

        def boom(*_a: object, **_k: object) -> Any:
            raise AssertionError("the oracle called production's missing-data logic")

        with monkeypatch.context() as mp:
            for name in ("missing_before", "_contiguous", "resume_after_downtime"):
                mp.setattr(PaperTradingService, name, boom)
            mp.setattr(PaperRuntime, "_attempt_backfill", boom)
            mp.setattr(paper_runtime, "validate_backfill", boom)
            alone = dataclasses.replace(r, restored=None, exposed=set())  # type: ignore[arg-type]
            assert recovery_evidence(alone, live_from_ms=live_from) == before


def test_the_oracle_catches_a_resume_the_existing_checks_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production mutant: `missing_before` never reports an exposed instrument's gap. At 0/19/10
    min the BTC bar of minute 13 never existed: the mutant resumes over it. The existing checks
    only look at bars that exist, so they pass; the oracle requires the bar and fails the resume."""
    monkeypatch.setattr(PaperTradingService, "missing_before", lambda self, f, extra=(): {})
    r = restore_after_crash(0, 19, 600_000)
    assert isinstance(r, Restart)
    out = outcome_after_downtime(r)
    assert r.restored.state is ServiceState.RUNNING
    assert recovery_outcome(r) == []  # blind to it
    assert out.kind == FAIL
    assert out.evidence.absent == {(minute(13), "BTCUSDT")}
    assert any("resumed without the evidence" in p for p in out.problems)


def test_the_journal_exposure_matches_the_books() -> None:
    """The oracle's exposure (journaled book events) equals production's books at the restore."""
    checked = 0
    for seed in range(8):
        for fb in range(0, 160, 13):
            r = restore_after_crash(seed, fb)
            if isinstance(r, str):
                continue
            assert journal_exposure(r.journal.batches()[: r.durable]) == r.exposed, (seed, fb)
            checked += 1
    assert checked >= 80


def test_the_oracle_ground_truth_is_the_uninterrupted_session() -> None:
    """`bar_store` (the oracle's truth) holds exactly the bars the uninterrupted session
    processed, plus none it did not."""
    for seed in (130741648, 50553820, 0):
        r = restore_after_crash(seed, 8)
        assert isinstance(r, Restart)
        ref = {
            (b[0].payload["bar"]["open_time_ms"], b[0].payload["bar"]["symbol"])
            for b in r.ref_journal.batches()
            if b[0].payload["kind"] == "bar"
        }
        read = bar_store(r)
        truth = {
            (b.open_time_ms, b.symbol)
            for market, symbol in (BTC, ETH)
            for b in read(market, symbol, minute(0), minute(40))
        }
        assert truth == ref


# ---------------------------------------------------------------- census (visibility)
def _census() -> Iterator[tuple[int, int, int]]:
    rng = random.Random(7)
    for seed in range(10):
        n = len(run_random_session(seed)[1].batches())
        for fb in sorted({rng.randrange(n) for _ in range(3)}):
            for delay in (60_000, 600_000):
                yield seed, fb, delay


#: delayed-restart outcomes over the fixed census (10 seeds, 3 crash points, 2 delays). The safe
#: blocks are a visible availability cost, not a success: every one leaves the session stopped
#: until an operator acts (M3 availability is not accepted).
CENSUS = {(RECOVERED, None): 45, (SAFE_BLOCK, INSTRUMENT_GAP): 15}  # 15 of 60 blocked


def test_delayed_restart_census() -> None:
    counts: Counter[tuple[str, str | None]] = Counter()
    failed = []
    for seed, fb, delay in _census():
        out = restart_outcome(seed, fb, delay)
        report_outcome("delayed restart census", out)
        counts[(out.kind, out.reason)] += 1
        if out.kind == FAIL:
            failed.append((seed, fb, delay, out.problems))
    assert failed == []
    assert dict(counts) == CENSUS
