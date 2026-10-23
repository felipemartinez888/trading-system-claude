"""M1 gate evidence: run the app with monitoring auto-started, then write a JSON report.

Usage (from the repository root):
    uv run python scripts/verify_live.py                # 2 hours, opens the dashboard
    uv run python scripts/verify_live.py --hours 0.25   # quick check
    uv run python scripts/verify_live.py --no-browser --out evidence/live

The app runs exactly as `tsclaude app` (same data directory, same configs), so the dashboard can be
watched during the run. At the end it quits cleanly and writes
`evidence/live/verify-<UTC>.json` with: stream uptime, reconnects, recycles, crashes, malformed
messages, gaps detected/resolved/open (by cause), stored bars per symbol vs expected minutes (by
source), clock-offset statistics and a verdict. Exit code: 0 pass, 1 monitoring did not run,
2 the run finished but the gate criteria are not met (see `verdict.failures`).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # allow running without an installed package
    sys.path.insert(0, str(SRC))

from tsclaude.api.context import AppContext  # noqa: E402
from tsclaude.api.server import RunOptions, run  # noqa: E402
from tsclaude.contracts.events import Event, EventType, NewEvent  # noqa: E402
from tsclaude.contracts.market import Market  # noqa: E402
from tsclaude.contracts.timeutil import MS_PER_MINUTE, SystemClock, to_iso  # noqa: E402
from tsclaude.marketdata.config import WatermarkConfig  # noqa: E402
from tsclaude.quality.gaps import GapRegistry  # noqa: E402
from tsclaude.runtime.settings import Settings  # noqa: E402
from tsclaude.store.eventstore import EventStore  # noqa: E402
from tsclaude.store.livebars import LiveBarStore  # noqa: E402

REPORT_VERSION = 1


@dataclass
class Observer:
    """Samples the running app every `sample_interval_s` (on the server's event loop)."""

    sample_interval_s: float = 60.0
    started_ms: int = field(default_factory=lambda: SystemClock().now_ms())
    samples: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] | None = None
    start_error: str | None = None

    async def on_sample(self, context: AppContext) -> None:
        snap = context.supervisor.snapshot() if context.supervisor else None
        if snap is None:
            self.samples.append({"at_ms": SystemClock().now_ms(), "running": False})
            return
        states: dict[str, int] = {}
        for s in snap.streams:
            key = str(s.state.value)
            states[key] = states.get(key, 0) + 1
        # Diagnostic: builder stats and bar counts per symbol
        builder_diag: dict[str, Any] = {}
        service = context.supervisor.service
        if service is not None and hasattr(service, "builder"):
            for b in service.builder.all():
                sym = f"{b.market}:{b.symbol}"
                builder_diag[sym] = {
                    "ws_closed": b.stats.ws_closed,
                    "ws_partial": b.stats.ws_partial,
                    "duplicates": b.stats.duplicates,
                    "late_recovered": b.stats.late_recovered,
                    "gap_minutes": b.stats.gap_minutes,
                    "cursor_ms": b.cursor_ms,
                    "settled_pending": len(b._settled),
                    "last_closed_open_ms": b.stats.last_closed_open_ms,
                }
        self.samples.append(
            {
                "at_ms": snap.as_of_ms,
                "running": snap.running,
                "clock_offset_ms": snap.clock_offset_ms.value,
                "stream_states": states,
                "builder_stats": builder_diag,
            }
        )

    async def on_finish(self, context: AppContext) -> None:
        await self.on_sample(context)
        snap = context.supervisor.snapshot() if context.supervisor else None
        if snap is not None:
            # Diagnostic: final builder stats
            final_builder: dict[str, Any] = {}
            service = context.supervisor.service if context.supervisor else None
            if service is not None and hasattr(service, "builder"):
                for b in service.builder.all():
                    sym = f"{b.market}:{b.symbol}"
                    final_builder[sym] = {
                        "ws_closed": b.stats.ws_closed,
                        "ws_partial": b.stats.ws_partial,
                        "duplicates": b.stats.duplicates,
                        "conflicts": b.stats.conflicts,
                        "late_recovered": b.stats.late_recovered,
                        "gap_minutes": b.stats.gap_minutes,
                        "invalid": b.stats.invalid,
                        "rest_errors": b.stats.rest_errors,
                        "cursor_ms": b.cursor_ms,
                        "watch_from_ms": b.watch_from_ms,
                        "settled_pending": len(b._settled),
                        "open_gaps": len(b.live_gaps()),
                        "caught_up": b.caught_up(),
                    }
            # Diagnostic: bar store counts per symbol
            bar_store_counts: dict[str, int] = {}
            if service is not None and hasattr(service, "_bars"):
                for b in service.builder.all():
                    bar_store_counts[f"{b.market}:{b.symbol}"] = service._bars.count(
                        b.market, b.symbol
                    )
            self.final = {
                "connections": [c.model_dump(mode="json") for c in snap.connections],
                "streams": [
                    {
                        "stream": s.stream,
                        "state": str(s.state.value),
                        "disconnects": s.disconnects,
                    }
                    for s in snap.streams
                ],
                "rest_banned": dict(snap.rest_banned),
                "rest_weight": context.supervisor.rest_usage() if context.supervisor else None,
                "demo_detector": vars(context.supervisor.detector.stats)
                if context.supervisor
                else None,
                "builder_stats": final_builder,
                "bar_store_counts": bar_store_counts,
            }

    def on_start_error(self, detail: str) -> None:
        self.start_error = detail


# ---------------------------------------------------------------------- report
def _intervals(events: list[Event], window: tuple[int, int]) -> list[tuple[int, int]]:
    """Monitoring intervals [start, end) that began inside the window."""
    open_by_id: dict[str, int] = {}
    out: list[tuple[int, int]] = []
    for e in events:
        mid = str(e.payload.get("monitoring_id"))
        if e.type is EventType.MONITORING_STARTED and window[0] <= e.ts_ms <= window[1]:
            open_by_id[mid] = e.ts_ms
        elif e.type is EventType.MONITORING_STOPPED and mid in open_by_id:
            out.append((open_by_id.pop(mid), e.ts_ms))
    out.extend((start, window[1]) for start in open_by_id.values())
    return out


def _uptime(events: list[Event], intervals: list[tuple[int, int]]) -> dict[str, dict[str, Any]]:
    """Per stream: share of monitored time in HEALTHY, from STREAM_STATE_CHANGED events."""
    result: dict[str, dict[str, Any]] = {}
    by_stream: dict[str, list[Event]] = {}
    for e in events:
        if e.type is EventType.STREAM_STATE_CHANGED:
            by_stream.setdefault(str(e.payload["stream"]), []).append(e)
    for stream, changes in sorted(by_stream.items()):
        healthy_ms = 0
        total_ms = 0
        transitions: dict[str, int] = {}
        for lo, hi in intervals:
            state = "CONNECTING"
            t = lo
            for e in changes:
                if e.ts_ms < lo:
                    state = str(e.payload["to"])
                    continue
                if e.ts_ms >= hi:
                    break
                if state == "HEALTHY":
                    healthy_ms += e.ts_ms - t
                t = e.ts_ms
                state = str(e.payload["to"])
                transitions[state] = transitions.get(state, 0) + 1
            if state == "HEALTHY":
                healthy_ms += hi - t
            total_ms += hi - lo
        result[stream] = {
            "healthy_ratio": round(healthy_ms / total_ms, 6) if total_ms else None,
            "healthy_s": healthy_ms / 1000,
            "monitored_s": total_ms / 1000,
            "transitions_into": transitions,
        }
    return result


# A minute's closed kline arrives shortly after it ends, and the watermark recovers it after
# `grace_s` (5 s); minutes that ended less than this before monitoring stopped may legitimately
# never have been closed, so they are not counted as expected.
CLOSE_ALLOWANCE_MS = 15_000


def _expected_minutes(intervals: list[tuple[int, int]]) -> list[int]:
    """Minutes entirely inside a monitoring interval (Lead decision L-02), minus the tail."""
    minutes: set[int] = set()
    for lo, hi in intervals:
        m = lo - lo % MS_PER_MINUTE + (MS_PER_MINUTE if lo % MS_PER_MINUTE else 0)
        while m + MS_PER_MINUTE + CLOSE_ALLOWANCE_MS <= hi:
            minutes.add(m)
            m += MS_PER_MINUTE
    return sorted(minutes)


def build_report(
    store: EventStore,
    bars: LiveBarStore,
    pairs: list[tuple[Market, str]],
    window: tuple[int, int],
    samples: list[dict[str, Any]],
    final: dict[str, Any] | None,
    start_error: str | None,
    retry_budget_ms: int,
    app_exit_code: int = 0,
) -> dict[str, Any]:
    """Build the report. `retry_budget_ms`: how long automatic recovery may take (attempts x
    retry interval + check interval); a gap still open after that means recovery failed."""
    events = store.read()
    intervals = _intervals(events, window)
    in_window = [e for e in events if window[0] <= e.ts_ms <= window[1]]
    registry = GapRegistry(_read_only, lambda: window[1])
    registry.load(store.read(types=[EventType.GAP_DETECTED, EventType.GAP_RESOLVED]))
    detected = [e for e in in_window if e.type is EventType.GAP_DETECTED]
    detected_ids = {str(e.payload["gap_id"]) for e in detected}
    resolved = [e for e in in_window if e.type is EventType.GAP_RESOLVED]
    open_gaps = [g for g in registry.open_gaps() if g.gap_id in detected_ids]

    expected = _expected_minutes(intervals)
    per_symbol: dict[str, Any] = {}
    for market, symbol in pairs:
        stored = {
            s.bar.open_time_ms: s
            for lo, hi in intervals
            for s in bars.range(market, symbol, lo - MS_PER_MINUTE, hi)
        }
        by_source: dict[str, int] = {}
        missing: list[int] = []
        for m in expected:
            s = stored.get(m)
            if s is None:
                missing.append(m)
            else:
                by_source[str(s.bar.source)] = by_source.get(str(s.bar.source), 0) + 1
        with_gap = {m for m in missing if registry.is_missing(market, symbol, m)}
        unexplained_minutes = [m for m in missing if m not in with_gap]
        per_symbol[f"{market}:{symbol}"] = {
            "expected_minutes": len(expected),
            "stored": len(expected) - len(missing),
            "by_source": by_source,
            "missing": len(missing),
            "missing_with_open_gap": len(with_gap),
            "missing_unexplained_count": len(unexplained_minutes),
            "missing_unexplained": [to_iso(m) for m in unexplained_minutes[:50]],  # sample
        }

    offsets = [s["clock_offset_ms"] for s in samples if s.get("clock_offset_ms") is not None]
    skew_events = [e.payload for e in in_window if e.type is EventType.CLOCK_SKEW_CHANGED]
    connections = (final or {}).get("connections", [])
    unexplained = sum(v["missing_unexplained_count"] for v in per_symbol.values())
    unknown_gaps = [g for g in open_gaps if str(g.cause) == "unknown"]
    # B2: an open gap only *explains* a missing minute while recovery may still be running. A gap
    # detected longer than the retry budget before the end of the window means REST recovery did
    # not work (or the exchange has no data) and fails the gate; the owner investigates it.
    overdue = [g for g in open_gaps if g.detected_at_ms < window[1] - retry_budget_ms]
    failures: list[str] = []
    if start_error is not None:
        failures.append(f"monitoring did not start: {start_error}")
    if not intervals:
        failures.append("no monitoring interval in the window")
    if unexplained:
        failures.append(f"{unexplained} expected minutes missing without an open gap")
    if unknown_gaps:
        failures.append(f"{len(unknown_gaps)} open gaps with cause 'unknown'")
    if overdue:
        failures.append(
            f"{len(overdue)} gaps still open longer than the retry budget "
            f"({retry_budget_ms // 1000} s): recovery did not complete"
        )
    if app_exit_code != 0:
        failures.append(f"the app exited with code {app_exit_code} (see logs/tsclaude.jsonl)")
    if any(c.get("crashes", 0) for c in connections):
        failures.append("WebSocket connection tasks crashed (see connections)")
    return {
        "report_version": REPORT_VERSION,
        "generated_at": to_iso(SystemClock().now_ms()),
        "window": {"start": to_iso(window[0]), "end": to_iso(window[1])},
        "monitoring_intervals": [{"start": to_iso(a), "end": to_iso(b)} for a, b in intervals],
        "stream_uptime": _uptime(events, intervals),
        "connections": connections,
        "totals": {
            "reconnects": sum(c.get("reconnects", 0) for c in connections),
            "recycles": sum(c.get("recycles", 0) for c in connections),
            "crashes": sum(c.get("crashes", 0) for c in connections),
            "malformed": sum(c.get("malformed", 0) for c in connections),
        },
        "gaps": {
            "detected": len(detected),
            "detected_minutes": sum(int(e.payload.get("minutes", 0)) for e in detected),
            "detected_by_cause": _count(str(e.payload.get("cause")) for e in detected),
            "resolution_events": len(resolved),
            "resolved_minutes": sum(int(e.payload.get("minutes", 0)) for e in resolved),
            "open": len(open_gaps),
            "open_minutes": sum(g.missing_minutes for g in open_gaps),
            "open_by_cause": _count(str(g.cause) for g in open_gaps),
            "retry_budget_s": retry_budget_ms / 1000,
            "open_beyond_retry_budget": [
                {
                    "gap_id": g.gap_id,
                    "market": str(g.market),
                    "symbol": g.symbol,
                    "start": to_iso(g.from_ms),
                    "end": to_iso(g.to_ms),
                    "missing_minutes": g.missing_minutes,
                    "cause": str(g.cause),
                    "detected_at": to_iso(g.detected_at_ms),
                }
                for g in overdue
            ],
        },
        "bars": per_symbol,
        "clock_offset_ms": {
            "samples": len(offsets),
            "min": min(offsets) if offsets else None,
            "max": max(offsets) if offsets else None,
            "mean": round(statistics.fmean(offsets), 3) if offsets else None,
            "last": offsets[-1] if offsets else None,
            "skew_events": skew_events,
        },
        "samples": samples,
        "final_snapshot": final,
        "verdict": {"pass": not failures, "failures": failures},
    }


def retry_budget_ms(watermark: WatermarkConfig) -> int:
    """Time automatic recovery may take: every retry attempt plus one watermark check."""
    return int(
        (
            watermark.gap_retry_max_attempts * watermark.gap_retry_interval_s
            + watermark.check_interval_s
            + watermark.grace_s
        )
        * 1000
    )


def _read_only(_: NewEvent) -> Event:
    raise RuntimeError("read-only gap projection")


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=2.0, help="run duration (default 2)")
    parser.add_argument("--sample-s", type=float, default=60.0, help="health sample interval")
    parser.add_argument("--out", type=Path, default=Path("evidence/live"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    update: dict[str, Any] = {}
    if args.no_browser:
        update["open_browser"] = False
    if args.port:
        update["port"] = args.port
    if update:
        settings = Settings.model_validate({**settings.model_dump(), **update})

    observer = Observer(sample_interval_s=args.sample_s)
    print(f"Verification run: {args.hours} h, data dir {settings.data_dir}, report -> {args.out}")
    code = run(
        settings,
        RunOptions(autostart_monitoring=True, run_for_s=args.hours * 3600, observer=observer),
    )
    finished = SystemClock().now_ms()
    from tsclaude.marketdata.config import load_config
    from tsclaude.runtime.supervisor import config_dir

    config = load_config(config_dir() / "universe.yaml", config_dir() / "data_quality.yaml")
    with EventStore(settings.state_db_path) as store, LiveBarStore(settings.state_db_path) as bars:
        report = build_report(
            store,
            bars,
            config.universe.pairs(),
            (observer.started_ms, finished),
            observer.samples,
            observer.final,
            observer.start_error,
            retry_budget_ms(config.quality.watermark),
            app_exit_code=code,
        )
    report["app_exit_code"] = code
    report["requested_hours"] = args.hours
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(finished / 1000))
    path = args.out / f"verify-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    verdict = report["verdict"]
    print(f"Report written to {path}")
    print("PASS" if verdict["pass"] else "FAIL: " + "; ".join(verdict["failures"]))
    if observer.start_error is not None or not report["monitoring_intervals"]:
        return 1
    return 0 if verdict["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
