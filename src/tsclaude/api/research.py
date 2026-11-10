"""Read-only research status endpoint (MVP): ``GET /api/research/status``.

Serves `tsclaude.research.status.load_status`: every registered strategy with its tier
(REFERENCE, VALIDATED, REJECTED, CANDIDATE, UNREGISTERED) and the overall state, which is
``NO_VALIDATED_STRATEGY`` unless a verified, preregistered run is graded A. The value is a
``SYSTEM`` value (ADR-0010) computed from files already on disk; nothing here runs a backtest or
changes a strategy.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock, to_iso


def research_router(runs_dir: Path, repo_root: Path, clock: WallClock) -> APIRouter:
    router = APIRouter()

    @router.get("/api/research/status")
    async def research_status() -> dict[str, Any]:
        from tsclaude.research.status import load_status

        now = clock.now_ms()
        try:
            value = await asyncio.to_thread(load_status, runs_dir, repo_root)
        except Exception as exc:  # fail closed: an unreadable status is unavailable, not "none"
            body = (
                Sourced[Any]
                .unavailable(f"research status unavailable: {type(exc).__name__}: {exc}")
                .model_dump()
            )
            return {"generated_at": to_iso(now), "status": body}
        body = Sourced[Any](value=value, provenance=Provenance.SYSTEM, as_of_ms=now).model_dump()
        return {"generated_at": to_iso(now), "status": body}

    return router
