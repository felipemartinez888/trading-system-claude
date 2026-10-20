"""HTML served at `/`: the built dashboard with the per-launch token injected, or a fallback page.

The token is injected server-side as `<meta name="tsc-token" content="...">`. Pages on other origins
cannot read it (no CORS headers are ever sent), and the Host allowlist blocks DNS rebinding.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

TOKEN_META_NAME = "tsc-token"  # noqa: S105 - a tag name, not a secret
_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)
_EXISTING_META = re.compile(r'<meta\s+name="tsc-token"[^>]*>\s*', re.IGNORECASE)


def inject_token(document: str, token: str) -> str:
    """Insert (or replace) the token meta tag in an HTML document."""
    meta = f'<meta name="{TOKEN_META_NAME}" content="{html.escape(token, quote=True)}">'
    document = _EXISTING_META.sub("", document)
    match = _HEAD_CLOSE.search(document)
    if match is None:
        return meta + document
    return document[: match.start()] + meta + document[match.start() :]


def load_index(static_dir: Path) -> str | None:
    index = static_dir / "index.html"
    if not index.is_file():
        return None
    return index.read_text(encoding="utf-8")


def is_safe_relative_path(rel_path: str) -> bool:
    r"""True only for a plain relative path made of normal segments.

    Checked lexically, *before* any filesystem access: on Windows, resolving an absolute UNC path
    such as ``//host/share/x`` or ``\\host\share\x`` would make an outbound SMB connection
    (and leak NTLM credentials) even if the result were rejected afterwards.
    """
    if not rel_path or "\x00" in rel_path or "\\" in rel_path or ":" in rel_path:
        return False
    for flavour in (PurePosixPath(rel_path), PureWindowsPath(rel_path)):
        if flavour.is_absolute() or flavour.anchor or flavour.drive:
            return False
        if any(part in ("..", ".") for part in flavour.parts):
            return False
    segments = rel_path.split("/")
    return all(_safe_segment(segment) for segment in segments)


# Windows device names: refused on every platform, case-insensitively, with or without an
# extension ("nul.js" also opens the NUL device on Windows).
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in (*"0123456789", "¹", "²", "³")}
)


def _safe_segment(segment: str) -> bool:
    if segment in ("", ".", ".."):
        return False
    # Windows strips trailing dots and spaces, so "index.html." / "app.js " would alias real
    # files and bypass exact-name checks; refuse them everywhere.
    if segment.endswith((".", " ")):
        return False
    stem = segment.split(".", 1)[0].rstrip(" ").upper()
    return stem not in _RESERVED_NAMES


def is_index_file(static_dir: Path, file: Path) -> bool:
    """True if `file` is the same file as `static_dir/index.html` (any alias of it)."""
    index = static_dir / "index.html"
    try:
        return index.is_file() and file.samefile(index)
    except OSError:
        return False


def is_index_path(rel_path: str) -> bool:
    """`index.html` in any letter case (case-insensitive filesystems would serve the same file)."""
    return rel_path.lower() == "index.html"


def resolve_static_file(static_dir: Path, rel_path: str) -> Path | None:
    """Return the file for `rel_path` inside `static_dir`, refusing anything that is not a plain
    relative path (traversal, absolute, drive, UNC, backslashes) without touching the filesystem.
    """
    if not is_safe_relative_path(rel_path):
        return None
    root = static_dir.resolve()
    try:
        candidate = (root / rel_path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def fallback_page(status: dict[str, Any]) -> str:
    """Shown when `src/tsclaude/web/static/index.html` is missing (bundle not built)."""
    status_json = html.escape(json.dumps(status, indent=2, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading System Claude — dashboard bundle not built</title>
<style>
  body {{ background:#0f1115; color:#e6e8ee; font:14px/1.5 system-ui, sans-serif; margin:2rem; }}
  code, pre {{ font-family: ui-monospace, Consolas, monospace; }}
  pre {{ background:#161a22; border:1px solid #2a3040; padding:1rem; overflow:auto; }}
  .warn {{ border-left:4px solid #e69f00; padding:.5rem 1rem; background:#1b1a14; }}
</style>
</head>
<body>
<h1>Dashboard bundle is not built</h1>
<p class="warn">The backend is running, but <code>src/tsclaude/web/static/index.html</code> does not
exist, so the dashboard cannot be shown. Build it with
<code>cd frontend &amp;&amp; pnpm install &amp;&amp; pnpm build</code>, then reload this page.</p>
<p>Current backend state from <code>/api/status</code> (captured when this page was served; reload
to refresh):</p>
<pre id="status">{status_json}</pre>
</body>
</html>
"""
