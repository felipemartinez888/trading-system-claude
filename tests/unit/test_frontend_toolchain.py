"""The dashboard's pnpm configuration works across the pnpm versions in use.

2026-11-10, owner smoke check on pnpm 12.18.0: pnpm 11 and later ignore the ``pnpm`` field of
package.json. ``pnpm.onlyBuiltDependencies`` was therefore not applied, and esbuild's install
script was blocked until the owner approved it by hand. CI pins pnpm 9, which runs every install
script, so CI could not catch this. These checks keep both declarations in place and identical.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _workspace() -> dict[str, object]:
    doc = yaml.safe_load((FRONTEND / "pnpm-workspace.yaml").read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_the_workspace_file_is_valid_for_pnpm_9() -> None:
    """pnpm 9 refuses a pnpm-workspace.yaml without a non-empty ``packages`` list."""
    assert _workspace()["packages"] == ["."]


def test_build_approvals_match_in_both_places() -> None:
    """pnpm >= 10.26 reads ``allowBuilds`` from the workspace file (pnpm 11+ only there);
    pnpm 10.0-10.25 read ``pnpm.onlyBuiltDependencies`` from package.json."""
    allow = _workspace()["allowBuilds"]
    assert isinstance(allow, dict)
    assert all(v is True for v in allow.values())  # only approvals, no silent denials
    manifest = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    assert sorted(manifest["pnpm"]["onlyBuiltDependencies"]) == sorted(allow)
    assert "esbuild" in allow
