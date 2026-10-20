// Types mirroring src/tsclaude/api/schemas.py and contracts/provenance.py.
// Hand-written for M0; generated from the OpenAPI schema once the API surface grows (ADR-0008).

export const PROVENANCES = [
  "LIVE",
  "SYSTEM",
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
  /** "starting" / "stopping" while market data is being started or stopped. */
  transition: string | null;
  /** Why the last start attempt failed (cleared by a successful start). */
  last_error: string | null;
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

/** Outcome of closing the app session, sent only after it was attempted. */
export interface ShutdownInfo {
  /** True only if session_stopped was written to the event log. */
  clean: boolean;
  reason: string;
  detail: string | null;
}

export interface QuitResponse {
  accepted: boolean;
  session: Sourced<ShutdownInfo>;
}

// ---------------------------------------------------------------- market data (M1)
// Contract models (Bar, BookTop, PartialCandle, Funding) keep epoch-millisecond fields.

export type Market = "spot" | "usdm";
export type StreamState = "CONNECTING" | "HEALTHY" | "STALE" | "DISCONNECTED" | "RECOVERING";
export type BarSource = "archive" | "rest_backfill" | "ws_live" | "rest_recovered" | "derived";

export interface Bar {
  market: Market;
  symbol: string;
  interval: string;
  open_time_ms: number;
  close_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  quote_volume: number;
  trades: number;
  source: BarSource;
}

export interface PartialCandle {
  market: Market;
  symbol: string;
  open_time_ms: number;
  close_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  event_time_ms: number;
}

export interface BookTop {
  bid_price: number;
  bid_qty: number;
  ask_price: number;
  ask_qty: number;
  received_at_ms: number;
}

export interface Funding {
  rate: number;
  next_funding_time_ms: number | null;
  source: string;
}

export interface MarketRow {
  market: Market;
  symbol: string;
  last_closed_bar: Sourced<Bar>;
  partial_candle: Sourced<PartialCandle>;
  best_bid_ask: Sourced<BookTop>;
  mark_price: Sourced<number>;
  funding: Sourced<Funding>;
  change_24h: Sourced<number>;
  freshness: Sourced<StreamState>;
  open_gap_minutes: Sourced<number>;
  exchange_status: Sourced<string>;
}

export interface MarketsResponse {
  generated_at: string;
  running: Sourced<boolean>;
  rows: MarketRow[];
}

export interface StreamStatus {
  stream: string;
  market: Market;
  symbol: string;
  kind: string;
  state: Sourced<StreamState>;
  since_ms: number;
  budget_ms: number;
  last_message_ms: number | null;
  last_fresh_ms: number | null;
  disconnects: number;
}

export interface ConnectionStatus {
  group: string;
  url: string | null;
  connected: boolean;
  connect_attempts: number;
  reconnects: number;
  recycles: number;
  malformed: number;
  messages: number;
  crashes: number;
  last_error: string | null;
}

export interface DemoDetectorInfo {
  enabled: boolean;
  threshold: number;
  bars_seen: number;
  emitted: number;
  skipped_unhealthy: number;
  skipped_not_live: number;
  last_skip_reason: string | null;
}

export interface StreamsHealthResponse {
  generated_at: string;
  running: Sourced<boolean>;
  streams: Sourced<StreamStatus[]>;
  connections: Sourced<ConnectionStatus[]>;
  clock_offset_ms: Sourced<number>;
  rest_weight: Sourced<Record<string, Record<string, [number, number | null]>>>;
  rest_banned: Sourced<Record<string, boolean>>;
  open_gaps: Sourced<number>;
  open_gap_minutes: Sourced<number>;
  demo_detector: Sourced<DemoDetectorInfo>;
}

export interface GapOut {
  gap_id: string;
  market: Market;
  symbol: string;
  interval: string;
  start: string;
  end: string;
  from_ms: number;
  to_ms: number;
  minutes: number;
  missing_minutes: number;
  missing_ranges_ms: [number, number][];
  cause: string;
  detected_at: string;
  resolution: string;
  resolved_at: string | null;
}

export interface GapsResponse {
  generated_at: string;
  gaps: Sourced<GapOut[]>;
}

export interface BarOut {
  open_time_ms: number;
  close_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source: BarSource;
  observed_session_id: string | null;
  provenance: Provenance;
}

export interface BarsResponse {
  generated_at: string;
  market: Market;
  symbol: string;
  interval: string;
  from_ms: number;
  to_ms: number;
  bars: Sourced<BarOut[]>;
  truncated: boolean;
}

export interface DemoSignalOut {
  signal_id: string;
  seq: number;
  created_at: string;
  decision_time: string;
  market: Market;
  symbol: string;
  timeframe: string;
  side: string;
  stage: string;
  ref_price: number;
  move: number;
  threshold: number;
  strategy_id: string;
  evidence_grade: string;
  run_mode: string;
  rationale: string;
}

export interface SignalsResponse {
  generated_at: string;
  signals: Sourced<DemoSignalOut[]>;
}

export type Severity = "info" | "warning" | "error";

export interface NotificationOut {
  id: string;
  seq: number;
  ts: string;
  kind: string;
  severity: Severity;
  title: string;
  detail: string;
  market: string | null;
  symbol: string | null;
  provenance: Provenance;
}

export interface NotificationsResponse {
  generated_at: string;
  notifications: Sourced<NotificationOut[]>;
  head_seq: number;
}

export interface DeltaMessage {
  as_of: string;
  markets: MarketRow[];
  notifications: NotificationOut[];
  health_changed: boolean;
}

export type LiveMessage =
  | { type: "status"; data: StatusResponse }
  | { type: "delta"; data: DeltaMessage }
  | { type: "shutting_down"; data: { reason: string } }
  | { type: "shutdown"; data: ShutdownInfo };
