// Types mirroring src/tsclaude/api/schemas.py and contracts/provenance.py.
// Hand-written for M0; generated from the OpenAPI schema once the API surface grows (ADR-0008).

export const PROVENANCES = [
  "LIVE",
  "PARTIAL",
  "RECOVERED",
  "HISTORICAL",
  "REPLAY",
  "BACKTEST",
  "SIMULATED",
  "ESTIMATED",
  "DEMO",
  "STALE",
  "UNAVAILABLE",
] as const;

export type Provenance = (typeof PROVENANCES)[number];

/** A value with where it came from (ADR-0010). `value` is null only when UNAVAILABLE. */
export interface Sourced<T> {
  value: T | null;
  provenance: Provenance;
  as_of_ms: number | null;
  reason: string | null;
}

export type MonitoringState = "STOPPED" | "RUNNING";

export interface SessionInfo {
  session_id: string;
  started_at: string;
  uptime_s: number;
}

export interface MonitoringInfo {
  state: MonitoringState;
  monitoring_id: string | null;
  since: string | null;
  start_allowed: boolean;
  blocked_reason: string | null;
}

export interface ChainInfo {
  ok: boolean;
  events_checked: number;
  head_seq: number;
  head_hash: string;
  first_bad_seq: number | null;
  reason: string | null;
  verified_at: string;
}

export type PreviousOutcome = "NONE" | "CLEAN" | "CRASHED" | "UNKNOWN";

export interface PreviousSessionInfo {
  outcome: PreviousOutcome;
  session_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  monitoring_was_active: boolean | null;
  unobserved_from: string | null;
  unobserved_to: string | null;
}

export interface StatusResponse {
  app_version: string;
  server_time: string;
  heartbeat_interval_s: number;
  session: Sourced<SessionInfo>;
  monitoring: Sourced<MonitoringInfo>;
  chain: Sourced<ChainInfo>;
  previous_session: Sourced<PreviousSessionInfo>;
  heartbeat: Sourced<string>;
  streams: Record<string, Sourced<string>>;
}

export interface CoverageInterval {
  monitoring_id: string;
  start: string;
  end: string | null;
  end_reason: string | null;
}

export interface CoverageGap {
  start: string;
  end: string | null;
  after_reason: string | null;
}

export interface CoverageResponse {
  generated_at: string;
  intervals: Sourced<CoverageInterval[]>;
  gaps: Sourced<CoverageGap[]>;
}

export interface EventOut {
  seq: number;
  ts: string;
  type: string;
  aggregate_type: string;
  aggregate_id: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface EventPage {
  items: EventOut[];
  head_seq: number;
  has_more: boolean;
}

export interface EventsResponse {
  generated_at: string;
  events: Sourced<EventPage>;
  chain: Sourced<ChainInfo>;
}

export interface QuitResponse {
  accepted: boolean;
}

export type LiveMessage =
  | { type: "status"; data: StatusResponse }
  | { type: "shutdown"; data: { reason: string } };
