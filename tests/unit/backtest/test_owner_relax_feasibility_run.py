"""The owner's H1 relaxation run sheet (docs/research/H1_RELAX_FEASIBILITY_RUN.md) matches the CLI.

The sheet applies the pre-stated selection rule of D-25 (M4-R1). Every ``uv run tsclaude ...``
command in its PowerShell blocks is parsed by the real parser. The count and power-plan commands
run as written against a small synthetic JSON-lines lake, and the plan fields that the T1-T3
read-out uses must exist. No real data is read here, and nothing in this test is evidence about H1.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import JsonLinesCodec, ms, synthetic_rows, write_lake

from tsclaude.cli import build_parser, main
from tsclaude.contracts.market import Market
from tsclaude.research import feasibility_cli
from tsclaude.store.holdout import HoldoutGuard
from tsclaude.store.lake_reader import LakeReader

REPO = Path(__file__).resolve().parents[3]
SHEET = REPO / "docs" / "research" / "H1_RELAX_FEASIBILITY_RUN.md"
GRID = REPO / "docs" / "research" / "grids" / "feasibility_h1_relax_probe.json"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT")
LADDER = (0.5, 0.75, 1.0)  # D-25, fixed before the count
TP_SET_B = "2.5,3,3.5"


def _powershell_lines() -> list[str]:
    text = SHEET.read_text(encoding="utf-8")
    blocks = re.findall(r"```powershell\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "the run sheet has no PowerShell block"
    return [line.strip() for block in blocks for line in block.splitlines()]


def _commands() -> list[str]:
    """Each ``uv run tsclaude`` invocation, cut at the first unquoted ``|``, ``;`` or ``}``."""
    out = []
    for line in _powershell_lines():
        start = line.find("uv run tsclaude")
        if start < 0:
            continue
        cmd, quote = [], None
        for ch in line[start:]:
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch in "|;}":
                break
            cmd.append(ch)
        out.append("".join(cmd).strip())
    return out


def _argv(command: str, **variables: str) -> list[str]:
    """PowerShell-style tokens with the sheet's variables substituted (longest name first)."""
    lex = shlex.shlex(command, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # Windows paths: a backslash is a path separator, not an escape
    tokens = list(lex)
    assert tokens[:3] == ["uv", "run", "tsclaude"], command
    out = []
    for token in tokens[3:]:
        for name, value in sorted(variables.items(), key=lambda kv: -len(kv[0])):
            token = token.replace(f"${name}", value)
        assert "$" not in token, f"unsubstituted variable in {token!r}"
        out.append(token.replace("\\", "/"))
    return out


def _by_subcommand() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for c in _commands():
        groups.setdefault(" ".join(c.split()[3:5]), []).append(c)
    return groups


def test_the_grid_is_exactly_the_preregistered_ladder() -> None:
    grid = json.loads(GRID.read_text(encoding="utf-8"))
    assert grid == [{"interval": "1h", "rank_max": r} for r in LADDER]


def test_the_sheet_runs_the_expected_commands() -> None:
    groups = _by_subcommand()
    assert len(groups["backtest feasibility"]) == 1
    assert len(groups["backtest power-plan"]) == 1  # inside the per-count loop
    assert len(groups["data verify"]) == 1
    assert "version" in {c.split()[3] for c in _commands()}
    count = groups["backtest feasibility"][0]
    assert "--grid docs\\research\\grids\\feasibility_h1_relax_probe.json" in count
    assert "--from 2022-02-01 --to 2025-01-01" in count
    assert "'spot:" + ",spot:".join(SYMBOLS) + "'" in count
    plan = groups["backtest power-plan"][0]
    for flag in ("--anchor 2022-02-01", "--embargo-minutes 4320", "--configurations 24"):
        assert flag in plan
    assert f"--tp-r '{TP_SET_B}'" in plan
    lines = "\n".join(_powershell_lines())
    assert "Start-Process" not in lines  # NORMAL user only: no elevation
    assert "RunAs" not in lines
    # a failed or empty step stops every later step, so no evidence zip is made from it
    blocks = re.findall(r"```powershell\n(.*?)```", SHEET.read_text(encoding="utf-8"), re.DOTALL)
    assert blocks[0].startswith("$stop = $false")
    assert all(b.startswith("if ($stop) {") for b in blocks[1:])
    assert "[cultureinfo]::InvariantCulture" in lines  # rank0.5, never rank0,5


def test_every_command_parses_with_the_real_cli(tmp_path: Path) -> None:
    parser = build_parser()
    variables = {"ev": str(tmp_path), "f.FullName": str(tmp_path / "f.json"), "tag": "1h_rank1",
                 "id": "d1", "n": "1"}  # fmt: skip
    for command in _commands():
        args = parser.parse_args(_argv(command, **variables))
        assert callable(getattr(args, "func", None)), command


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    rows = synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 11))
    write_lake(data, [(Market.SPOT, s, rows) for s in SYMBOLS])
    monkeypatch.setenv("TSC_DATA_DIR", str(data))
    monkeypatch.chdir(REPO)  # the sheet runs from the worktree root (relative --grid path)

    def reader(d: Path, guard: HoldoutGuard) -> LakeReader:
        return LakeReader.open(d, guard, JsonLinesCodec())

    monkeypatch.setattr(feasibility_cli, "_default_reader", reader)
    return data


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, Any]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_the_sheet_rehearses_end_to_end_on_a_synthetic_lake(
    lake: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ev = tmp_path / "ev"
    ev.mkdir()
    groups = _by_subcommand()

    # step 2: one count, three rank_max values; files named by the count's own rank_max
    code, summary = _run(capsys, _argv(groups["backtest feasibility"][0], ev=str(ev)))
    assert code == 0
    assert summary["ok"] is True
    assert summary["trials_recorded"] == 0
    assert summary["pnl_free"] is True
    files: dict[float, Path] = {}
    for result in summary["results"]:
        doc = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        assert doc["strategy"]["interval"] == "1h"
        assert doc["segment"] == "development"
        assert doc["draft"] is True
        assert sorted(doc["symbols"]) == sorted(f"spot:{s}" for s in SYMBOLS)
        rank = doc["strategy"]["params"]["rank_max"]
        files[rank] = ev / f"feasibility_1h_rank{rank:g}.json"
        files[rank].write_text(json.dumps(doc), encoding="utf-8")
    assert sorted(files) == list(LADDER)

    # step 4: one power plan per count, take-profit set B, with the fields T1-T3 read
    for rank, path in files.items():
        tag = path.stem.replace("feasibility_", "")
        argv = _argv(groups["backtest power-plan"][0], ev=str(ev), tag=tag,
                     **{"f.FullName": str(path)})  # fmt: skip
        code, plan = _run(capsys, argv)
        assert code == 0, rank
        assert plan["ok"] is True
        written = json.loads((ev / f"power_{tag}.json").read_text(encoding="utf-8"))
        assert written["pnl_free"] is True
        assert [g["tp_r"] for g in written["geometries"]] == [2.5, 3.0, 3.5]
        assert written["trials"]["configurations"] == 24
        assert written["trials"]["folds"] == 7
        tw = written["training_windows"]
        assert "selection_possible_in_every_fold" in tw  # T1
        assert "candidates_ceiling" in tw["folds"][0]
        assert {f["embargo_minutes"] for f in written["folds"]} == {4320}
        assert isinstance(written["oos_candidates"], int | float)  # T2
        for g in written["geometries"]:  # T3 reads n_eff_ratio 1.0
            first = g["by_n_eff"][0]
            assert first["n_eff_ratio"] == 1.0
            assert "min_detectable_margin" in first
