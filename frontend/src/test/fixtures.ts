// Test fixtures shaped like real /api responses (replay of what the backend returns; not shown in
// the app).

import type {
  MarketRow,
  MonitoringState,
  Provenance,
  Sourced,
  StatusResponse,
} from "../api/types";

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
      transition: null,
      last_error: null,
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
    streams:
      state === "RUNNING"
        ? { spot: system("HEALTHY 6/6"), futures: system("HEALTHY 9/9") }
        : {
            spot: unavailable("monitoring has not been started in this app session"),
            futures: unavailable("monitoring has not been started in this app session"),
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

export function sourced<T>(value: T | null, provenance: Provenance, reason: string | null = null): Sourced<T> {
  if (provenance === "UNAVAILABLE") return { value: null, provenance, as_of_ms: null, reason };
  return { value, provenance, as_of_ms: AS_OF, reason };
}

/** A market row whose market-data values all carry `provenance` (as the API would send them). */
export function marketRow(
  symbol: string,
  provenance: Provenance,
  market: "spot" | "usdm" = "spot",
): MarketRow {
  const unavailable = provenance === "UNAVAILABLE";
  return {
    market,
    symbol,
    last_closed_bar: sourced(
      {
        market,
        symbol,
        interval: "1m",
        open_time_ms: AS_OF - 60_000,
        close_time_ms: AS_OF - 1,
        open: 100,
        high: 101,
        low: 99,
        close: 100.5,
        volume: 1,
        quote_volume: 100,
        trades: 3,
        source: "ws_live",
      },
      provenance,
      unavailable ? "no closed bar stored yet" : null,
    ),
    partial_candle: sourced(
      {
        market,
        symbol,
        open_time_ms: AS_OF,
        close_time_ms: AS_OF + 59_999,
        open: 100.5,
        high: 100.7,
        low: 100.4,
        close: 100.6,
        volume: 0.5,
        event_time_ms: AS_OF + 10_000,
      },
      provenance === "LIVE" ? "PARTIAL" : provenance,
      unavailable ? "no forming candle received" : null,
    ),
    best_bid_ask: sourced(
      { bid_price: 100.5, bid_qty: 1, ask_price: 100.6, ask_qty: 2, received_at_ms: AS_OF },
      provenance,
      unavailable ? "no bookTicker received" : null,
    ),
    mark_price: sourced(100.55, provenance, unavailable ? "no markPrice received" : null),
    funding: sourced(
      { rate: 0.0001, next_funding_time_ms: AS_OF + 3_600_000, source: "ws_mark_price" },
      provenance,
      unavailable ? "no funding data received" : null,
    ),
    change_24h: sourced(0.0123, provenance, unavailable ? "no stored 1m bar 24 h before" : null),
    freshness: sourced("HEALTHY", "SYSTEM"),
    open_gap_minutes: sourced(0, "SYSTEM"),
    exchange_status: sourced("TRADING", "HISTORICAL"),
  };
}
