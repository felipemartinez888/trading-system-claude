"""Read-only agent status endpoint (M5): ``GET /api/agents/status``.

Serves `tsclaude.agents.status.agent_status` as a ``SYSTEM`` value (ADR-0010): configuration
(mode, model, cap; invalid config is reported and means mode off), prompt versions and the day's
spend from the persisted cost ledger. Nothing here builds a client or calls an API.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock, to_iso


def agents_router(config_path: Path, agents_dir: Path, clock: WallClock) -> APIRouter:
    router = APIRouter()

    @router.get("/api/agents/status")
    async def agents_status() -> dict[str, Any]:
        from tsclaude.agents.status import agent_status

        now = clock.now_ms()
        try:
            value = await asyncio.to_thread(agent_status, config_path, agents_dir, now)
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
