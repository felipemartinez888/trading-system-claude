// Test fixtures shaped like real /api responses (replay of what the backend returns; not shown in
// the app).

import type { MonitoringState, Sourced, StatusResponse } from "../api/types";

export const TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz0123456789";
const AS_OF = Date.parse("2026-10-20T12:00:00.000Z");

/** The app's own runtime state carries SYSTEM provenance (not LIVE, which is market data). */
export function system<T>(value: T): Sourced<T> {
  return { value, provenance: "SYSTEM", as_of_ms: AS_OF, reason: null };
}

function unavailable<T>(reason: string): Sourced<T> {
  return { value: null, provenance: "UNAVAILABLE", as_of_ms: null, reason };
}

export function statusFixture(
  state: MonitoringState,
  overrides: Partial<StatusResponse> = {},
): StatusResponse {
  return {
    app_version: "0.1.0",
    server_time: "2026-10-20T12:00:00.000Z",
    heartbeat_interval_s: 5,
    session: system({
      session_id: "01K80000000000000000000000",
      started_at: "2026-10-20T11:00:00.000Z",
      uptime_s: 3600,
    }),
    monitoring: system({
      state,
      monitoring_id: state === "RUNNING" ? "01K80000000000000000000001" : null,
      since: state === "RUNNING" ? "2026-10-20T11:30:00.000Z" : null,
      start_allowed: state === "STOPPED",
      blocked_reason: null,
    }),
    chain: system({
      ok: true,
      events_checked: 12,
      head_seq: 12,
      head_hash: "a".repeat(64),
      first_bad_seq: null,
      reason: null,
      verified_at: "2026-10-20T11:00:00.000Z",
    }),
    previous_session: system({
      outcome: "NONE",
      session_id: null,
      started_at: null,
      ended_at: null,
      end_reason: null,
      monitoring_was_active: null,
      unobserved_from: null,
      unobserved_to: null,
    }),
    heartbeat: system("2026-10-20T12:00:00.000Z"),
    streams: {
      spot: unavailable("not implemented until M1"),
      futures: unavailable("not implemented until M1"),
    },
    ...overrides,
  };
}

export function setToken(token: string = TOKEN): void {
  const meta = document.createElement("meta");
  meta.name = "tsc-token";
  meta.content = token;
  document.head.appendChild(meta);
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
