"""Built dashboard bundle location.

`static/` is produced by `pnpm build` in `frontend/` (Vite `outDir`) and is never committed;
CI builds it. When it is missing the API serves a fallback page that says so.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
