"""Code provenance recorded in every import manifest (M2-E3, C-1 prevention).

The canonical datasets of 2026-10-24 record only `tsclaude 0.1.0`, so it cannot be told which
code produced them (C-1). From now on every manifest says which commit of the *imported package*
ran and whether its working tree had uncommitted changes to tracked files. Git is asked about the
directory the `tsclaude` package was loaded from, not the process's working directory: an import
may run code from another worktree. HEAD is trusted only if the package's `__init__.py` is a
tracked file of that checkout (a copy installed inside an ignored directory of a repository is not
attributed to the repository). Unknown values are recorded as `None`, never guessed. Git is run with
`--no-optional-locks`, so it never takes the index lock away from the owner's own git commands.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

import tsclaude

GIT_TIMEOUT_S = 10.0
LIBRARIES = ("pyarrow", "duckdb", "httpx")


def _git(git: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "--no-optional-locks", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def code_provenance() -> dict[str, Any]:
    """Commit and dirty state of the code that runs this import (cached per process)."""
    package_dir = Path(tsclaude.__file__).resolve().parent
    out: dict[str, Any] = {
        "tsclaude_version": tsclaude.__version__,
        "package_path": str(package_dir),
        "git_commit": None,
        "git_dirty": None,
        "git_dirty_tracked_files": None,
        "git_untracked_package_files": None,
        "python": platform.python_version(),
        "libraries": {name: _version(name) for name in LIBRARIES},
    }
    git = shutil.which("git")
    if git is None:
        return out
    tracked = _git(git, package_dir, "ls-files", "--error-unmatch", "__init__.py")
    if tracked is None or tracked.returncode != 0:
        return out  # not a tracked checkout of the package (e.g. an installed copy): unknown
    head = _git(git, package_dir, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return out
    out["git_commit"] = head.stdout.strip() or None
    status = _git(git, package_dir, "status", "--porcelain", "--untracked-files=no")
    if status is not None and status.returncode == 0:
        changed = [line for line in status.stdout.splitlines() if line.strip()]
        out["git_dirty"] = bool(changed)
        out["git_dirty_tracked_files"] = len(changed)
    untracked = _git(git, package_dir, "ls-files", "--others", "--exclude-standard", "--", ".")
    if untracked is not None and untracked.returncode == 0:
        out["git_untracked_package_files"] = len(
            [line for line in untracked.stdout.splitlines() if line.strip()]
        )
    return out


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None
