"""Paper-trading endpoints (MVP-2): read-only status, books and fills, plus one operator action.

Reads come from the runtime's last published snapshot (taken on the event loop), never from live
objects. Labels (ADR-0010): the runtime state is ``SYSTEM``; book values and fills are
``SIMULATED`` while paper is RUNNING, otherwise ``STALE`` (last known values, stamped with the time
they were published). Nothing here can place an order.

``POST /api/paper/session/new`` (token-protected, note required, idempotent by ``request_id``)
records a new paper session that opens at the next monitoring start; it is refused while paper is
processing and never overwrites an existing session.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.params import Depends
from pydantic import BaseModel, ConfigDict, Field

from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock, to_iso
from tsclaude.runtime.paper_runtime import PaperActionRefused, PaperRuntime, PaperState

NOT_CONFIGURED = "paper trading is not configured in this app instance"


class NewPaperSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    note: str = Field(min_length=1, max_length=500)
    request_id: str = Field(pattern=r"^[0-9A-Za-z_-]{8,64}$")


def _values(snap: dict[str, Any], at: int | None, value: Any) -> dict[str, Any]:
    """SIMULATED while running, else STALE with the publication time."""
    if snap.get("service") is None:
        reason = snap.get("reason") or f"paper is {snap.get('state')}"
        return Sourced[Any].unavailable(f"no paper session: {reason}").model_dump()
    if snap.get("state") == str(PaperState.RUNNING):
        return Sourced[Any](value=value, provenance=Provenance.SIMULATED, as_of_ms=at).model_dump()
    return Sourced[Any](
        value=value,
        provenance=Provenance.STALE,
        as_of_ms=at if at is not None else 0,
        reason=f"paper is {snap.get('state')}: last known values",
    ).model_dump()


def paper_router(
    get_runtime: Callable[[], PaperRuntime | None],
    clock: WallClock,
    token_dep: Sequence[Depends],
) -> APIRouter:
    router = APIRouter()

    def _snapshot() -> tuple[dict[str, Any], int | None] | None:
        runtime = get_runtime()
        return None if runtime is None else runtime.published()

    @router.get("/api/paper/status")
    def paper_status() -> dict[str, Any]:
        now = clock.now_ms()
        got = _snapshot()
        if got is None:
            body = Sourced[Any].unavailable(NOT_CONFIGURED).model_dump()
        else:
            snap, at = got
            service = snap.get("service") or {}
            value = {
                k: snap.get(k)
                for k in (
                    "state",
                    "reason",
                    "enabled",
                    "settings_source",
                    "strategy",
                    "session_id",
                    "next_session_id",
                    "observing_since_ms",
                    "dropped_updates",
                    "backfill",
                    "limitations",
                )
            }
            value.update(
                {
                    "service_state": service.get("state"),
                    "fault": service.get("fault"),
                    "awaiting_missing": service.get("awaiting_missing"),
                    "last_minute_ms": service.get("last_minute_ms"),
                    "batches": service.get("batches"),
                    "clock": service.get("clock"),
                }
            )
            body = Sourced[Any](value=value, provenance=Provenance.SYSTEM, as_of_ms=at).model_dump()
        return {"generated_at": to_iso(now), "status": body}

    @router.get("/api/paper/books")
    def paper_books() -> dict[str, Any]:
        now = clock.now_ms()
        got = _snapshot()
        if got is None:
            books = Sourced[Any].unavailable(NOT_CONFIGURED).model_dump()
        else:
            snap, at = got
            service = snap.get("service") or {}
            value = {
                "books": snap.get("books"),
                "summary": service.get("books"),
                "ledger_hashes": service.get("ledger_hashes"),
            }
            books = _values(snap, at, value)
        return {"generated_at": to_iso(now), "books": books}

    @router.get("/api/paper/fills")
    def paper_fills(limit: int = Query(100, ge=1, le=200)) -> dict[str, Any]:
        now = clock.now_ms()
        got = _snapshot()
        if got is None:
            fills = Sourced[Any].unavailable(NOT_CONFIGURED).model_dump()
        else:
            snap, at = got
            fills = _values(snap, at, list(snap.get("fills") or [])[-limit:])
        return {"generated_at": to_iso(now), "fills": fills}

    # async: runs on the event loop, like the paper consumer, so the runtime is never mutated
    # from a worker thread
    @router.post("/api/paper/session/new", dependencies=list(token_dep))
    async def new_paper_session(body: NewPaperSession) -> dict[str, Any]:
        runtime = get_runtime()
        if runtime is None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=NOT_CONFIGURED)
        try:
            return runtime.request_new_session(body.note, body.request_id)
        except PaperActionRefused as exc:
            raise HTTPException(exc.code, detail=exc.detail) from exc

    return router
