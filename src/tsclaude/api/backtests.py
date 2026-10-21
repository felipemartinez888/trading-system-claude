"""Read-only backtest endpoints (M2-T4): ``GET /api/backtests`` and ``GET /api/backtests/{id}``.

Values come from ``<data_dir>/runs/<run_id>/`` written by `research.runner`; every value is
labelled ``BACKTEST`` (ADR-0010), and ``metrics_verified`` states whether ``metrics.json`` still
matches the SHA-256 recorded in its manifest. Run ids are validated and never leave the runs
directory.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status

from tsclaude.contracts.provenance import Provenance, Sourced

_RUN_ID = re.compile(r"^bt-[0-9A-Za-z-]{1,80}$")


def backtests_router(runs_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/backtests")
    async def list_backtests() -> dict[str, Any]:
        from tsclaude.research.runner import list_runs

        runs = await asyncio.to_thread(list_runs, runs_dir)
        return {
            "provenance": str(Provenance.BACKTEST),
            "runs": [
                Sourced[dict[str, Any]](value=r, provenance=Provenance.BACKTEST).model_dump()
                for r in runs
            ],
        }

    @router.get("/api/backtests/{run_id}")
    async def get_backtest(run_id: str) -> dict[str, Any]:
        from tsclaude.research.runner import load_run

        if not _RUN_ID.match(run_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown run")
        run = await asyncio.to_thread(load_run, runs_dir, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown run")
        return {
            "provenance": str(Provenance.BACKTEST),
            "summary": Sourced[dict[str, Any]](
                value=run["summary"], provenance=Provenance.BACKTEST
            ).model_dump(),
            "manifest": Sourced[dict[str, Any]](
                value=run["manifest"], provenance=Provenance.BACKTEST
            ).model_dump(),
            "metrics": (
                Sourced[dict[str, Any]](value=run["metrics"], provenance=Provenance.BACKTEST)
                if run["metrics"] is not None
                else Sourced[dict[str, Any]].unavailable("metrics.json missing")
            ).model_dump(),
        }

    return router
