"""Read-only agent status for ``GET /api/agents/status`` (computed from files on disk only).

The agent gate is not wired into the paper runtime yet, so this reports configuration and the
persisted cost ledger, plus ``runtime_wired: false``; it never builds a client, never reads the
API key's value and never calls anything.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from .budget import MS_PER_DAY, LedgerUnreadable, q, read_ledger
from .config import load_config
from .gate import LEDGER_FILE
from .prompt_registry import Role, load_prompt
from .synthesis import SYNTHESIS_VERSION

RUNTIME_NOTE = (
    "The agent gate is not wired into the paper runtime: no signal is reviewed in any mode yet. "
    "The real Anthropic adapter is a stub; cost and latency on live signals are not measured."
)


def agent_status(config_path: Path, agents_dir: Path, now_ms: int) -> dict[str, Any]:
    loaded = load_config(config_path)
    cfg = loaded.config
    day = now_ms // MS_PER_DAY
    ledger: dict[str, Any]
    try:
        entries = read_ledger(agents_dir / LEDGER_FILE)
    except LedgerUnreadable as exc:
        ledger = {"error": f"cost ledger unreadable ({exc})"}
    else:
        today = [e for e in entries if e.day == day]
        spent = q(sum((e.cost_usd for e in today), Decimal(0)))
        ledger = {
            "error": None,
            "calls_today": len(today),
            "spent_today_usd": str(spent),
            "remaining_today_usd": str(max(Decimal(0), q(cfg.daily_cap_usd - spent))),
            "calls_total": len(entries),
            "spent_total_usd": str(q(sum((e.cost_usd for e in entries), Decimal(0)))),
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
        "runtime_wired": False,
        "note": RUNTIME_NOTE,
    }
