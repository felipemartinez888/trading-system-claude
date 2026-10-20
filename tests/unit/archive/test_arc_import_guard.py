"""CI guard (L-07 b): only store/ may import the lake internals, pyarrow or duckdb."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "tsclaude"
FORBIDDEN = ("tsclaude.store.lake", "pyarrow", "duckdb")
ALLOWED_DIR = SRC / "store"


def _hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]  # importlib.import_module("...") and similar
        for name in names:
            for bad in FORBIDDEN:
                if name == bad or name.startswith(bad + "."):
                    out.append(f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')} {name}")
    return out


def test_only_store_imports_lake_pyarrow_duckdb() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if ALLOWED_DIR in path.parents:
            continue
        offenders.extend(_hits(path))
    assert offenders == [], "lake internals imported outside store/: " + "; ".join(offenders)


def test_guard_detects_offences(tmp_path: Path) -> None:
    p = tmp_path / "x.py"
    p.write_text(
        "import pyarrow.parquet\nfrom tsclaude.store.lake import Lake\n"
        "import importlib\nimportlib.import_module('duckdb')\n",
        encoding="utf-8",
    )
    global SRC
    old = SRC
    SRC = tmp_path
    try:
        assert len(_hits(p)) >= 3
    finally:
        SRC = old
