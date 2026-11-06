"""Recorded-session replay verification on a short synthetic paper session.

Synthetic validation of the tool only: it does not satisfy the real recorded-session gate.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from paper_helpers import BTC, ScriptedStrategy, config, drive, record, two_symbol_bars

from tsclaude.cli import main
from tsclaude.runtime.session_evidence import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    capture_session_end,
    verify_recorded_session,
)
from tsclaude.sim.paper import PaperTradingService
from tsclaude.sim.testing import minute
from tsclaude.store.eventstore import EventStore

CFG = config()
SESSION = "synthetic"
SCRIPT = {(minute(2), "BTCUSDT"): [record(1, 2)]}


def _record(data_dir: Path, *, minutes: int = 8, evidence: bool = True) -> PaperTradingService:
    bars = two_symbol_bars(14)
    with EventStore(data_dir / "state.db") as store:
        svc = PaperTradingService.open(
            CFG, store, session_id=SESSION, strategy=ScriptedStrategy(SCRIPT)
        )
        drive(svc, bars, list(range(minutes)))
        assert svc.fault is None
        if evidence:
            capture_session_end(
                data_dir,
                config=CFG,
                session_id=SESSION,
                ledger_hashes=svc.ledger_hashes(),
                fingerprint=svc.session.fingerprint(),
                batches=svc.journal.batches(),
                state="RUNNING",
                written_at_ms=1_000,
            )
    return svc


def _files(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_a_faithful_session_passes_and_the_original_is_untouched(tmp_path: Path) -> None:
    live = _record(tmp_path)
    before = _files(tmp_path)
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (PASS, 0)
    assert rep.details["replayed_ledger_hashes"] == live.ledger_hashes()
    assert set(rep.details["replayed_ledger_hashes"]) >= {"baseline", "shadow"}
    assert _files(tmp_path) == before


def test_a_tampered_expected_hash_fails(tmp_path: Path) -> None:
    _record(tmp_path)
    f = next((tmp_path / "session_end").glob("*.json"))
    doc = json.loads(f.read_text())
    doc["ledger_hashes"]["baseline"] = "0" * 64
    f.write_text(json.dumps(doc))
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (FAIL, 1)
    assert any("baseline" in r for r in rep.reasons)


def test_a_missing_book_in_the_evidence_fails(tmp_path: Path) -> None:
    _record(tmp_path)
    f = next((tmp_path / "session_end").glob("*.json"))
    doc = json.loads(f.read_text())
    del doc["ledger_hashes"]["shadow"]
    f.write_text(json.dumps(doc))
    assert verify_recorded_session(tmp_path, CFG).verdict == FAIL


def test_corrupted_evidence_fails(tmp_path: Path) -> None:
    _record(tmp_path)
    next((tmp_path / "session_end").glob("*.json")).write_text("{not json")
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (FAIL, 1)


def test_no_evidence_is_inconclusive_never_pass(tmp_path: Path) -> None:
    _record(tmp_path, evidence=False)
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (INCONCLUSIVE, 2)


def test_no_session_is_inconclusive(tmp_path: Path) -> None:
    assert verify_recorded_session(tmp_path, CFG).verdict == INCONCLUSIVE


def test_an_edited_journal_fails_on_the_hash_chain(tmp_path: Path) -> None:
    _record(tmp_path)
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute("DROP TRIGGER events_no_update")  # a tamperer with file access
    con.execute("UPDATE events SET ts_ms = ts_ms + 1 WHERE seq = 3")
    con.commit()
    con.close()
    rep = verify_recorded_session(tmp_path, CFG)
    assert rep.verdict == FAIL
    assert any("journal" in r for r in rep.reasons)


def test_a_session_that_ran_past_its_evidence_is_inconclusive(tmp_path: Path) -> None:
    """Evidence written after minute 5; the run continued without a clean stop."""
    bars = two_symbol_bars(14)
    with EventStore(tmp_path / "state.db") as store:
        svc = PaperTradingService.open(
            CFG, store, session_id=SESSION, strategy=ScriptedStrategy(SCRIPT)
        )
        drive(svc, bars, list(range(5)))
        capture_session_end(
            tmp_path,
            config=CFG,
            session_id=SESSION,
            ledger_hashes=svc.ledger_hashes(),
            fingerprint=svc.session.fingerprint(),
            batches=svc.journal.batches(),
            state="RUNNING",
            written_at_ms=1_000,
        )
        drive(svc, bars, [5, 6, 7])
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (INCONCLUSIVE, 2)
    assert "past the evidence" in " ".join(rep.reasons)


def test_a_different_configuration_is_not_comparable(tmp_path: Path) -> None:
    _record(tmp_path)
    other = config(decision_latency_ms=1)
    assert verify_recorded_session(tmp_path, other).verdict == INCONCLUSIVE


def test_the_evidence_file_is_never_overwritten(tmp_path: Path) -> None:
    _record(tmp_path)
    with pytest.raises(FileExistsError):
        _record_again(tmp_path)


def _record_again(d: Path) -> None:
    capture_session_end(
        d,
        config=CFG,
        session_id=SESSION,
        ledger_hashes={},
        fingerprint="x",
        batches=[],
        state="RUNNING",
        written_at_ms=1_000,
    )


def test_the_command_reports_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`tsclaude verify-replay` with the repository's configs: the synthetic session was recorded
    with another configuration, so the verdict is a non-zero INCONCLUSIVE, not a crash."""
    _record(tmp_path)
    code = main(["verify-replay", "--data-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["verdict"] == INCONCLUSIVE


_ = BTC


def test_a_session_that_did_not_end_running_is_not_a_pass(tmp_path: Path) -> None:
    bars = two_symbol_bars(14)
    with EventStore(tmp_path / "state.db") as store:
        svc = PaperTradingService.open(
            CFG, store, session_id=SESSION, strategy=ScriptedStrategy(SCRIPT)
        )
        drive(svc, bars, list(range(5)))
        capture_session_end(
            tmp_path,
            config=CFG,
            session_id=SESSION,
            ledger_hashes=svc.ledger_hashes(),
            fingerprint=svc.session.fingerprint(),
            batches=svc.journal.batches(),
            state="FAULTED",
            written_at_ms=1_000,
        )
    rep = verify_recorded_session(tmp_path, CFG)
    assert (rep.verdict, rep.exit_code) == (INCONCLUSIVE, 2)
    assert "FAULTED" in " ".join(rep.reasons)


def test_a_bad_batch_count_is_malformed_not_a_crash(tmp_path: Path) -> None:
    _record(tmp_path)
    f = next((tmp_path / "session_end").glob("*.json"))
    doc = json.loads(f.read_text())
    doc["batches"] = "7"
    f.write_text(json.dumps(doc))
    assert verify_recorded_session(tmp_path, CFG).verdict == FAIL


def test_the_instance_lock_file_does_not_disturb_the_verification(tmp_path: Path) -> None:
    _record(tmp_path)
    (tmp_path / "tsclaude.lock").write_text("")
    assert verify_recorded_session(tmp_path, CFG).verdict == PASS


def test_an_unexpected_error_in_the_command_is_inconclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tsclaude.runtime.session_evidence as ev

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ev, "verify_recorded_session", boom)
    monkeypatch.setattr("tsclaude.runtime.paper_runtime.build_paper_config", lambda *_a, **_k: CFG)
    assert main(["verify-replay", "--data-dir", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == INCONCLUSIVE


def test_a_pass_without_observed_bars_or_fills_says_that_no_trading_was_exercised(
    tmp_path: Path,
) -> None:
    """Nothing is observed in this session: the replay reproduces, but nothing traded."""
    bars = two_symbol_bars(14)
    with EventStore(tmp_path / "state.db") as store:
        svc = PaperTradingService.open(
            CFG, store, session_id=SESSION, strategy=ScriptedStrategy({}), observation_gated=True
        )
        drive(svc, bars, list(range(4)))
        capture_session_end(
            tmp_path,
            config=CFG,
            session_id=SESSION,
            ledger_hashes=svc.ledger_hashes(),
            fingerprint=svc.session.fingerprint(),
            batches=svc.journal.batches(),
            state="RUNNING",
            written_at_ms=1_000,
        )
    rep = verify_recorded_session(tmp_path, CFG)
    assert rep.verdict == PASS
    assert rep.details["trading_exercised"] is False
    assert rep.details["strategy_books_exercised"] is False
    assert all(f["entries"] == 0 for f in rep.details["simulated_fills"].values())
    assert "NO TRADING EXERCISED" in " ".join(rep.reasons)


def test_a_pass_with_strategy_entries_reports_strategy_books_exercised(tmp_path: Path) -> None:
    _record(tmp_path)
    rep = verify_recorded_session(tmp_path, CFG)
    assert rep.verdict == PASS
    fills = rep.details["simulated_fills"]
    assert fills["baseline"]["entries"] > 0
    assert rep.details["observed_bars"] > 0
    assert rep.details["trading_exercised"] is True
    assert rep.details["strategy_books_exercised"] is True


def test_benchmark_only_fills_do_not_count_as_strategy_execution(tmp_path: Path) -> None:
    """No strategy script: only the benchmark book may fill (if the bars were observed)."""
    bars = two_symbol_bars(14)
    with EventStore(tmp_path / "state.db") as store:
        svc = PaperTradingService.open(
            CFG, store, session_id=SESSION, strategy=ScriptedStrategy({})
        )
        drive(svc, bars, list(range(8)))
        capture_session_end(
            tmp_path,
            config=CFG,
            session_id=SESSION,
            ledger_hashes=svc.ledger_hashes(),
            fingerprint=svc.session.fingerprint(),
            batches=svc.journal.batches(),
            state="RUNNING",
            written_at_ms=1_000,
        )
    rep = verify_recorded_session(tmp_path, CFG)
    assert rep.verdict == PASS
    fills = rep.details["simulated_fills"]
    assert fills["baseline"]["entries"] == 0
    assert fills["shadow"]["entries"] == 0
    assert rep.details["strategy_books_exercised"] is False
    assert fills["benchmark"]["entries"] > 0
    assert rep.details["trading_exercised"] is True
    assert "only the benchmark book traded" in " ".join(rep.reasons)
