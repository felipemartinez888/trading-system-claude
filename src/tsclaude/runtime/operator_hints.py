"""Actionable operator hints for paper-runtime blockers (presentation only, MVP).

The paper runtime already publishes *why* it is blocked (observation codes, states, reasons). This
module turns the stable codes into one sentence saying what the operator can do about it. It reads
the published snapshot only, changes nothing, and never turns a block into a pass: every hint for a
blocked state repeats that bars are processed unobserved, so no entry is created or filled.

Owner evidence for the most frequent case (2026-11-06 and 2026-11-09 sessions): the Windows clock
was 6.9 s and 3.4 s off Binance server time, above `clock_skew_max_ms` (1000 ms in
`configs/risk.yaml`), so every bar of those sessions was processed unobserved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: What the operator can do when the computer clock is off (no administrator rights needed).
CLOCK_SYNC_STEPS = (
    "Sync the computer clock with internet time: Windows Settings > Time & language > "
    "Date & time > 'Sync now' (no administrator rights needed), then press Stop and Start "
    "monitoring."
)

UNOBSERVED = (
    "Until then every bar is processed unobserved: no entry is created or filled (fail closed)."
)

_CLOCK_RETRY = (
    "The app retries automatically with a growing delay; check the internet connection if it "
    "persists."
)

_OBSERVATION_HINTS: dict[str, str] = {
    "clock_offset_skew": (
        "The computer clock differs from Binance server time by more than the allowed limit "
        "(clock_skew_max_ms in configs/risk.yaml). " + CLOCK_SYNC_STEPS
    ),
    "clock_resync_failed": "No fresh Binance server-clock reading could be taken. " + _CLOCK_RETRY,
    "clock_resync_unavailable": (
        "Market data offers no server-clock reading in this configuration, so this run cannot "
        "observe. Restart the app; report it if it persists."
    ),
    "clock_round_stale": "The last Binance server-clock reading is too old. " + _CLOCK_RETRY,
    "clock_round_not_fresh": "The Binance server-clock reading was not fresh. " + _CLOCK_RETRY,
    "clock_offset_unknown": "The Binance server-clock offset is unknown yet. " + _CLOCK_RETRY,
    "clock_rtt_unknown": "The Binance server-clock round trip is unknown yet. " + _CLOCK_RETRY,
    "clock_discontinuity": (
        "The computer clock jumped (sleep/resume, or a time sync during the run). The app "
        "retries automatically; keep the computer awake while paper runs."
    ),
    "clock_step_in_round": (
        "The computer clock moved during the server-clock reading (a time sync while it ran). "
        "The app retries automatically."
    ),
    "clock_rtt_negative": (
        "The computer clock stepped back during the server-clock reading. The app retries "
        "automatically."
    ),
    "clock_continuity_unknown": (
        "No monotonic clock evidence for the server-clock reading yet. " + _CLOCK_RETRY
    ),
    "clock_continuity_other_round": "Clock evidence came from different readings. " + _CLOCK_RETRY,
    "clock_rtt_other_round": "Clock evidence came from different readings. " + _CLOCK_RETRY,
    "clock_evidence_inconsistent": "The server-clock evidence is inconsistent. " + _CLOCK_RETRY,
    "clock_round_future": (
        "The server-clock reading is dated in the future: the computer clock stepped back. "
        + CLOCK_SYNC_STEPS
    ),
    "no_snapshot": "No market-data snapshot yet: is monitoring started? " + _CLOCK_RETRY,
}


def observation_hint(observation: Mapping[str, Any] | None) -> str | None:
    """A hint for a blocked observation, or None when it is active, pending or unknown."""
    if not observation or observation.get("state") != "blocked":
        return None
    code = observation.get("code")
    if code == "clock_resync_pending":  # transient, by design: nothing to do yet
        return None
    hint = _OBSERVATION_HINTS.get(str(code)) if code else None
    if hint is None:
        hint = f"Observation is blocked ({code or 'unknown reason'}). Report it if it persists."
    return f"{hint} {UNOBSERVED}"


def paper_hint(snapshot: Mapping[str, Any]) -> str | None:
    """The most useful single hint for the published paper snapshot, or None."""
    state = str(snapshot.get("state") or "")
    reason = str(snapshot.get("reason") or "")
    if state == "UNAVAILABLE" and "history" in reason and "not complete" in reason:
        return (
            "The required market-data history is not complete. Press Stop, then Start again in "
            "the same data directory: stored bars and checkpoints are reused and only the rest is "
            "fetched (see /api/ingestion). Paper never opens on a partial history."
        )
    if state == "AWAITING_BACKFILL":
        return (
            "Paper is waiting for missing minutes after a restart (see 'Awaiting missing'). It "
            "resumes by itself when the market-data backfill supplies them; nothing is traded "
            "meanwhile."
        )
    if state == "RUNNING":
        return observation_hint(snapshot.get("observation"))
    return None


__all__ = ["CLOCK_SYNC_STEPS", "UNOBSERVED", "observation_hint", "paper_hint"]
