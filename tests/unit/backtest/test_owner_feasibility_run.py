"""The owner's H1-1 run sheet (docs/research/H1_FEASIBILITY_RUN.md) matches the CLI.

Every ``uv run tsclaude ...`` command in the sheet's PowerShell blocks is parsed by the real
parser, and the count and power-plan commands are executed as written against a small synthetic
JSON-lines lake. The commands have to keep working when the CLI changes. No real data is read
here, and no result in this test is evidence about H1.
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
SHEET = REPO / "docs" / "research" / "H1_FEASIBILITY_RUN.md"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT")
TP_SETS = ("2,2.5,3", "2.5,3,3.5")


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


def _argv(command: str, ev: Path, **variables: str) -> list[str]:
    """PowerShell-style tokens (single and double quotes; backslashes are literal) with the
    sheet's variables substituted, as the tsclaude argument list."""
    lex = shlex.shlex(command, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # Windows paths: a backslash is a path separator, not an escape
    tokens = list(lex)
    assert tokens[:3] == ["uv", "run", "tsclaude"], command
    values = {"ev": str(ev), **variables}
    out = []
    for token in tokens[3:]:
        for name, value in sorted(values.items(), key=lambda kv: -len(kv[0])):
            token = token.replace(f"${name}", value)
        assert "$" not in token, f"unsubstituted variable in {token!r}"
        out.append(token.replace("\\", "/"))
    return out


def _by_subcommand() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for c in _commands():
        tokens = c.split()
        groups.setdefault(" ".join(tokens[3:5]), []).append(c)
    return groups


def test_the_sheet_runs_the_expected_commands() -> None:
    groups = _by_subcommand()
    assert len(groups["backtest feasibility"]) == 1
    assert len(groups["backtest power-plan"]) == 2  # 15m and 1h, inside the take-profit loop
    assert len(groups["data verify"]) == 1
    assert "version" in {c.split()[3] for c in _commands()}
    # both take-profit sets of the decision package, quoted for PowerShell
    lines = "\n".join(_powershell_lines())
    for tp in TP_SETS:
        assert f"'{tp}'" in lines
    assert "'spot:" + ",spot:".join(SYMBOLS) + "'" in lines


def test_every_command_parses_with_the_real_cli(tmp_path: Path) -> None:
    parser = build_parser()
    for command in _commands():
        argv = _argv(command, tmp_path, tp=TP_SETS[0], tag="2_2.5_3", id="d1", n="1")
        args = parser.parse_args(argv)
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

    # step 3: one count, both intervals; the files are told apart by their content
    code, summary = _run(capsys, _argv(groups["backtest feasibility"][0], ev))
    assert code == 0
    assert summary["ok"] is True
    assert summary["trials_recorded"] == 0
    assert summary["pnl_free"] is True
    by_interval: dict[str, dict[str, Any]] = {}
    for result in summary["results"]:
        doc = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        by_interval[doc["strategy"]["interval"]] = doc
        (ev / f"feasibility_{doc['strategy']['interval']}.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )
    assert set(by_interval) == {"15m", "1h"}
    for doc in by_interval.values():
        assert doc["segment"] == "development"
        assert doc["draft"] is True
        assert sorted(doc["symbols"]) == sorted(f"spot:{s}" for s in SYMBOLS)
        assert len(doc["dataset_ids"]) == len(SYMBOLS)  # step 4 verifies exactly these

    # step 5: four power plans, written by --out as UTF-8 JSON
    for tp in TP_SETS:
        tag = tp.replace(",", "_")
        for command in groups["backtest power-plan"]:
            code, plan = _run(capsys, _argv(command, ev, tp=tp, tag=tag))
            assert code == 0, command
            assert plan["ok"] is True
            assert [g["tp_r"] for g in plan["geometries"]] == [float(x) for x in tp.split(",")]
            assert plan["trials"]["folds"] == 7
            assert plan["trials"]["n_trials_planned"] == 199
    for interval, embargo_min in (("15m", 1440), ("1h", 4320)):
        for tp in TP_SETS:
            path = ev / f"power_{interval}_tp{tp.replace(',', '_')}.json"
            plan = json.loads(path.read_text(encoding="utf-8"))  # plain UTF-8, no BOM
            assert plan["source_content_hash"] == by_interval[interval]["content_hash"]
            assert {f["embargo_minutes"] for f in plan["folds"]} == {embargo_min}
