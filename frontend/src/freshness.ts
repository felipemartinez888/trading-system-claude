// Client-side freshness: the UI may only *downgrade* provenance, never upgrade it (ADR-0010).
// A value is shown as STALE when the last status snapshot is too old (backend unreachable or the
// push channel is down) or when the backend itself reports its heartbeat as overdue.

import { useEffect, useState } from "react";

import { STATUS_POLL_MS, useStatus } from "./api/hooks";
import type { Provenance, Sourced } from "./api/types";

/** Client-side freshness budget for the last status snapshot, in heartbeats. */
const CLIENT_STALE_HEARTBEATS = 3;

export interface Freshness {
  stale: boolean;
  reason: string | null;
}

export interface Shown {
  provenance: Provenance;
  reason: string | null;
  asOfMs: number | null;
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function useBackendFreshness(): Freshness {
  const status = useStatus();
  const now = useNow();
  const data = status.data;
  if (data === undefined) return { stale: false, reason: null };
  // Fresh = pushed within a few heartbeats or polled within two polling periods (push may be down).
  const budgetMs = Math.max(
    data.heartbeat_interval_s * 1000 * CLIENT_STALE_HEARTBEATS,
    2 * STATUS_POLL_MS + 5_000,
  );
  if (status.isError || now - status.dataUpdatedAt > budgetMs) {
    return { stale: true, reason: "no fresh update from the backend" };
  }
  if (data.heartbeat.provenance === "STALE") {
    return { stale: true, reason: "the backend heartbeat is overdue" };
  }
  return { stale: false, reason: null };
}

/** Provenance to display for a value, downgraded to STALE when the backend is not fresh. */
export function shown<T>(sourced: Sourced<T>, freshness: Freshness): Shown {
  const downgradable = sourced.provenance === "LIVE" || sourced.provenance === "SYSTEM";
  if (freshness.stale && downgradable) {
    return { provenance: "STALE", reason: freshness.reason, asOfMs: sourced.as_of_ms };
  }
  return { provenance: sourced.provenance, reason: sourced.reason, asOfMs: sourced.as_of_ms };
}
