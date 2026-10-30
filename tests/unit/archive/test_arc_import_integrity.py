"""M2-E3: importer integrity safeguards (C-1 prevention), with small synthetic archives.

C-1: two traded rows present in verified source archives were missing from the canonical lake, and
no committed parser reproduces the loss. These tests inject such losses at every stage and require
the import to fail closed (nothing committed), plus code provenance in every manifest."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from arc_helpers import FakeArchive, JsonLinesCodec, kline_values, ms

import tsclaude
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.archive import csvparse, provenance
from tsclaude.marketdata.archive.importer import (
    ArchiveImportError,
    Importer,
    ImportRequest,
    source_data_lines,
)
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period
from tsclaude.store.lake import Lake, LakeError, LakeKind, Staging
from tsclaude.store.lake_writer import LakeWriter

NOW = ms(2026, 10, 20, 12, 30)
SYM = "BTCUSDT"
JAN = ArchiveFile(Market.SPOT, ArchiveDataType.KLINES, SYM, "1m", Period.MONTHLY, "2024-01")
REQ = ImportRequest(Market.SPOT, LakeKind.BARS_1M, SYM, ms(2024, 1, 1), ms(2024, 2, 1))


def _import(lake: Lake, archive: FakeArchive) -> Any:
    writer = LakeWriter(lake.data_dir, lake.codec)
    return asyncio.run(Importer(writer, archive, None, ManualClock(NOW)).run(REQ))


def _nothing_committed(lake: Lake) -> None:
    lake.refresh()
    assert lake.dataset_ids() == []
    published = lake.data_dir / "lake"
    assert not published.exists() or not list(published.rglob("*.jsonl"))  # no part published
    staging = lake.data_dir / ".staging"
    assert not staging.exists() or not any(staging.iterdir())


# ---------------------------------------------------------------------- per-file reconciliation
def test_a_row_lost_by_the_parser_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C-1 shape: a traded row silently missing after parsing a verified archive."""
    archive.add_klines(JAN)
    original = csvparse.parse_klines_csv

    def lossy(text: str, *a: Any, **kw: Any) -> Any:
        parsed = original(text, *a, **kw)
        rows = list(parsed.rows)
        del rows[3924]  # like XRP 2025-04 line 3925
        return type(parsed)(rows, parsed.unit, parsed.issues)

    monkeypatch.setattr(csvparse, "parse_klines_csv", lossy)
    with pytest.raises(
        ArchiveImportError, match="source row reconciliation failed: 44640 data lines"
    ):
        _import(lake, archive)
    _nothing_committed(lake)


def test_a_newline_lost_in_the_first_decompression_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two records merged into one line parse as one valid row; only an independent count of the
    verified bytes sees the loss."""
    archive.add_klines(JAN)
    original = csvparse.read_single_csv

    def merged(data: bytes, name: str | None = None) -> str:
        text = original(data, name)
        cut = text.index("\n", 1000)
        return text[:cut] + "," + text[cut + 1 :]  # 23 cells; the parser reads the first 12

    monkeypatch.setattr(csvparse, "read_single_csv", merged)
    with pytest.raises(ArchiveImportError, match="source row reconciliation failed"):
        _import(lake, archive)
    _nothing_committed(lake)


def test_malformed_rows_and_headers_keep_the_accounting_exact(
    lake: Lake, archive: FakeArchive
) -> None:
    t = ms(2024, 1, 10)
    bad = kline_values(t, 60_000)
    bad[6] = t + 41_646  # truncated candle, like 2023-03-24 12:39
    from arc_helpers import kline_lines

    archive.add(JAN, kline_lines(JAN.start_ms, JAN.end_ms, override={t: bad}), header=True)
    res = _import(lake, archive)
    assert res.dataset_id is not None
    src = next(s for s in res.sources if s.get("type") == "archive_monthly")
    assert src["header_row"] is True
    assert src["rows"] == 44_639
    assert src["source_data_lines"] == 44_640  # rows + 1 malformed; the header is not data
    assert res.qa["summary"]["malformed_rows"] == 1


def test_independent_line_count_ignores_blank_lines_and_the_header() -> None:
    from arc_helpers import make_zip

    header = next(iter(csvparse.KNOWN_HEADERS))
    data = make_zip("x.csv", f"{header}\r\n1,2\r\n\r\n , ,\n3,4\n5,6")
    assert source_data_lines(data) == (3, True)  # CRLF, blank and comma-only lines handled
    unknown = make_zip("y.csv", "open_time,open\n1,2\n3,4")
    assert source_data_lines(unknown) == (3, False)  # not a known header: counted (malformed)
    assert source_data_lines(make_zip("z.csv", "1,2\n3,4")) == (2, False)


def test_a_first_row_dropped_as_a_fake_header_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review B-1: a parser that drops the first data row and reports it as a header must not be
    trusted by the reconciliation."""
    archive.add_klines(JAN)
    original = csvparse.parse_klines_csv

    def fake_header(text: str, *a: Any, **kw: Any) -> Any:
        parsed = original(text, *a, **kw)
        parsed.issues.header_skipped = True
        return type(parsed)(list(parsed.rows)[1:], parsed.unit, parsed.issues)

    monkeypatch.setattr(csvparse, "parse_klines_csv", fake_header)
    with pytest.raises(
        ArchiveImportError, match="header detected independently=False, by the parser=True"
    ):
        _import(lake, archive)
    _nothing_committed(lake)


# ---------------------------------------------------------------------- per-month accounting
def test_a_row_lost_while_merging_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive.add_klines(JAN)
    original = Importer._merge

    def lossy(self: Importer, *a: Any, **kw: Any) -> Any:
        rows = original(self, *a, **kw)
        return rows[:-1]

    monkeypatch.setattr(Importer, "_merge", lossy)
    with pytest.raises(
        ArchiveImportError, match="2024-01: row accounting failed: parsed 44640 != kept 44639"
    ):
        _import(lake, archive)
    _nothing_committed(lake)


def test_a_row_lost_while_staging_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive.add_klines(JAN)
    original = Staging.write_parts

    def lossy(self: Staging, market: Any, kind: Any, symbol: str, rows: Any) -> Any:
        return original(self, market, kind, symbol, list(rows)[1:])

    monkeypatch.setattr(Staging, "write_parts", lossy)
    with pytest.raises(ArchiveImportError, match="staged rows 44639 != valid rows 44640"):
        _import(lake, archive)
    _nothing_committed(lake)


# ---------------------------------------------------------------------- re-read before publishing
def test_a_staged_part_changed_after_writing_is_never_published(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive.add_klines(JAN)
    original = Staging.commit

    def tamper(self: Staging, meta: Any, qa: Any) -> Any:
        staged = self._staged[0][1]
        lines = staged.read_text(encoding="utf-8").splitlines()
        staged.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # one row vanishes
        return original(self, meta, qa)

    monkeypatch.setattr(Staging, "commit", tamper)
    with pytest.raises(LakeError, match="changed after it was written"):
        _import(lake, archive)
    _nothing_committed(lake)


def test_a_part_that_reads_back_differently_is_never_published(
    tmp_path: Path, archive: FakeArchive
) -> None:
    class DroppingCodec(JsonLinesCodec):
        """Writes every row, reads one fewer back (a faulty read path)."""

        def read(self, path: Path, schema: Any) -> list[Any]:
            return super().read(path, schema)[:-1]

    lake = Lake(tmp_path / "data", DroppingCodec())
    archive.add_klines(JAN)
    with pytest.raises(LakeError, match="does not read back as written"):
        _import(lake, archive)
    _nothing_committed(lake)


def test_an_existing_corrupted_part_is_not_silently_adopted(
    lake: Lake, archive: FakeArchive
) -> None:
    archive.add_klines(JAN)
    first = _import(lake, archive)
    part = lake.data_dir / first.parts[0].path
    lines = part.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[100])
    row["close"] = row["close"] + 1.0  # still a valid row, different content
    lines[100] = json.dumps(row)
    part.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.rmtree(lake.imports_dir)  # force a fresh commit of the same content
    with pytest.raises(LakeError, match="does not read back as written"):
        _import(lake, archive)
    lake.refresh()
    assert lake.dataset_ids() == []  # no manifest refers to the corrupted part


# ---------------------------------------------------------------------- code provenance
def test_every_manifest_records_the_importing_code(lake: Lake, archive: FakeArchive) -> None:
    archive.add_klines(JAN)
    res = _import(lake, archive)
    manifest = lake.manifest(res.dataset_id)
    prov = manifest["code_provenance"]
    assert set(prov) == {
        "tsclaude_version",
        "package_path",
        "git_commit",
        "git_dirty",
        "git_dirty_tracked_files",
        "git_untracked_package_files",
        "python",
        "libraries",
    }
    assert prov["tsclaude_version"] == tsclaude.__version__
    assert set(prov["libraries"]) == {"pyarrow", "duckdb", "httpx"}
    assert manifest["tool"] == f"tsclaude {tsclaude.__version__}"  # unchanged field kept
    git = shutil.which("git")
    pkg = Path(tsclaude.__file__).resolve().parent
    if git is None:
        pytest.skip("git not available: provenance values cannot be cross-checked")
    head = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=pkg, capture_output=True, text=True, check=False
    )
    if head.returncode != 0:
        pytest.skip("package is not a git checkout here")
    assert prov["git_commit"] == head.stdout.strip()
    status = subprocess.run(
        [git, "status", "--porcelain", "--untracked-files=no"],
        cwd=pkg,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    assert prov["git_dirty"] is bool(changed)
    assert prov["git_dirty_tracked_files"] == len(changed)


def test_unknown_provenance_is_recorded_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provenance.code_provenance.cache_clear()
    monkeypatch.setattr(provenance.shutil, "which", lambda _name: None)
    try:
        prov = provenance.code_provenance()
        assert prov["git_commit"] is None
        assert prov["git_dirty"] is None
        assert prov["tsclaude_version"] == tsclaude.__version__
    finally:
        provenance.code_provenance.cache_clear()


def test_a_non_checkout_records_no_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    provenance.code_provenance.cache_clear()
    monkeypatch.setattr(provenance, "_git", lambda *a, **kw: None)
    try:
        assert provenance.code_provenance()["git_commit"] is None
    finally:
        provenance.code_provenance.cache_clear()
