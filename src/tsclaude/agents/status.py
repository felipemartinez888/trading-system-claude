"""Read-only agent status for ``GET /api/agents/status``.

Configuration and the persisted cost ledger come from files on disk; the per-session counts come
from the paper runtime's last published snapshot (``runtime``, M5-3), labelled on their own
(ADR-0010): ``SYSTEM`` with the publication time, or ``UNAVAILABLE`` with the reason. This never
builds a client, never reads the API key's value and never calls anything.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from tsclaude.contracts.provenance import Provenance, Sourced

from .budget import MS_PER_DAY, LedgerUnreadable, calls, over_reservations, q, read_ledger, spent_on
from .config import load_config
from .gate import LEDGER_FILE
from .prompt_registry import Role, load_prompt
from .synthesis import SYNTHESIS_VERSION

RUNTIME_NOTE = (
    "The agent gate is wired into the paper runtime: a paper session created while the mode is "
    "confirmed has an agent_gated book that takes only signals with an APPROVED review; the "
    "baseline book is never affected (L-12). Mode off builds nothing. The real Anthropic adapter "
    "is a stub, so outside tests every review is AI_UNAVAILABLE and agent_gated takes nothing; "
    "cost and latency on live signals are not measured."
)

type PaperSnapshot = tuple[dict[str, Any], int | None]


def runtime_counts(published: PaperSnapshot | None) -> dict[str, Any]:
    """Per-session agent counts from the paper runtime's published snapshot, as a `Sourced`
    dump: SYSTEM (the app's own runtime state) or UNAVAILABLE with the reason."""
    if published is None:
        return Sourced[Any].unavailable("paper trading is not configured").model_dump()
    snap, at = published
    agents = snap.get("agents")
    if not isinstance(agents, dict):
        why = (
            "no paper session is open"
            if snap.get("service") is None
            else "the open paper session has no agent_gated book (agents mode was not "
            "confirmed when it was created)"
        )
        return Sourced[Any].unavailable(why).model_dump()
    value = {"paper_state": snap.get("state"), **agents}
    return Sourced[Any](value=value, provenance=Provenance.SYSTEM, as_of_ms=at).model_dump()


def agent_status(
    config_path: Path,
    agents_dir: Path,
    now_ms: int,
    runtime: PaperSnapshot | None = None,
) -> dict[str, Any]:
    loaded = load_config(config_path)
    cfg = loaded.config
    day = now_ms // MS_PER_DAY
    ledger: dict[str, Any]
    try:
        entries = read_ledger(agents_dir / LEDGER_FILE)
    except LedgerUnreadable as exc:
        ledger = {"error": f"cost ledger unreadable ({exc})"}
    else:
        per_call = calls(entries)
        spent = spent_on(entries, day)
        ledger = {
            "error": None,
            "calls_today": sum(1 for c in per_call.values() if c.day == day),
            "unsettled_today": sum(
                1 for c in per_call.values() if c.day == day and c.settled is None
            ),
            "spent_today_usd": str(spent),
            "remaining_today_usd": str(max(Decimal(0), q(cfg.daily_cap_usd - spent))),
            "calls_total": len(per_call),
            "spent_total_usd": str(q(sum((c.spend for c in per_call.values()), Decimal(0)))),
            "over_reservation_calls": over_reservations(entries),
        }
    return {
        "mode": str(cfg.mode),
        "config_ok": loaded.ok,
        "config_errors": list(loaded.errors),
        "config_version": cfg.version,
        "model": cfg.model,
        "daily_cap_usd": str(q(cfg.daily_cap_usd)),
        "per_call_timeout_s": cfg.per_call_timeout_s,
        "prompts": {str(r): load_prompt(r).ref for r in Role},
        "synthesis_version": SYNTHESIS_VERSION,
        "ledger": ledger,
        "runtime_wired": True,
        "runtime": runtime_counts(runtime),
        "note": RUNTIME_NOTE,
    }
