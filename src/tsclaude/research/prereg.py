"""Preregistration id check for backtest runs (Lead decision on the M2-T4 audit, finding 1).

A run may carry ``prereg_id`` only if ``preregistrations/<prereg_id>.yaml`` is tracked by git in
the repository and has no uncommitted changes. Otherwise the id is refused (fail closed): a
free-form id would let a run look preregistered without being so. The holdout unlock performs its
own, stricter verification inside `store.holdout`; this check is for every other segment.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

PREREG_DIR = "preregistrations"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class PreregistrationRefused(ValueError):
    pass


def check_preregistration(repo_root: Path | str, prereg_id: str) -> str:
    """Return the repository-relative path of the committed preregistration or raise."""
    if not _ID.fullmatch(prereg_id):
        raise PreregistrationRefused(f"invalid preregistration id {prereg_id!r}")
    rel = f"{PREREG_DIR}/{prereg_id}.yaml"
    root = Path(repo_root)
    if not (root / rel).is_file():
        raise PreregistrationRefused(f"{rel} does not exist in {root}")
    git = shutil.which("git")
    if git is None:
        raise PreregistrationRefused("git is not available; cannot verify the preregistration")
    tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(root), "ls-files", "--error-unmatch", rel],
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:
        raise PreregistrationRefused(f"{rel} is not committed")
    clean = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(root), "diff", "--quiet", "HEAD", "--", rel],
        capture_output=True,
        text=True,
        check=False,
    )
    if clean.returncode != 0:
        raise PreregistrationRefused(f"{rel} has uncommitted changes")
    return rel
