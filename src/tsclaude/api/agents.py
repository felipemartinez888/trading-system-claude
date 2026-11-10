"""Read-only agent status endpoint (M5): ``GET /api/agents/status``.

Serves `tsclaude.agents.status.agent_status` as a ``SYSTEM`` value (ADR-0010): configuration
(mode, model, cap; invalid config is reported and means mode off), prompt versions and the day's
spend from the persisted cost ledger, plus the paper session's agent counts (``runtime``, its own
``SYSTEM`` / ``UNAVAILABLE`` label, from the paper runtime's published snapshot; M5-3). Nothing here
builds a client or calls an API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock, to_iso

type PaperSnapshot = tuple[dict[str, Any], int | None]


def agents_router(
    config_path: Path,
    agents_dir: Path,
    clock: WallClock,
    paper: Callable[[], PaperSnapshot | None] | None = None,
) -> APIRouter:
    """`paper` returns the paper runtime's last published snapshot (`PaperRuntime.published`),
    or None when no paper runtime exists."""
    router = APIRouter()

    @router.get("/api/agents/status")
    async def agents_status() -> dict[str, Any]:
        from tsclaude.agents.status import agent_status

        now = clock.now_ms()
        published = None if paper is None else paper()  # an immutable snapshot (event loop)
        try:
            value = await asyncio.to_thread(agent_status, config_path, agents_dir, now, published)
        except Exception as exc:  # fail closed: unreadable status is unavailable, not "off"
            body = (
                Sourced[Any]
                .unavailable(f"agent status unavailable: {type(exc).__name__}")
                .model_dump()
            )
            return {"generated_at": to_iso(now), "status": body}
        body = Sourced[Any](value=value, provenance=Provenance.SYSTEM, as_of_ms=now).model_dump()
        return {"generated_at": to_iso(now), "status": body}

    return router
