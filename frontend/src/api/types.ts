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

// ---------------------------------------------------------------------- paper (MVP-2..4)
// /api/paper/*: runtime state is SYSTEM; book values, fills, activity and reference signals are
// SIMULATED / STALE / UNAVAILABLE. Decimal amounts arrive as exact strings.

export interface PaperWarmup {
  strategy_bars: number;
  needed: number;
  ready: boolean;
  history_breaks?: number;
  last_break_detected_at_ms?: number | null;
  last_break_phase?: string | null;
  last_break_reason?: string | null;
}

export interface PaperReference {
  id: string;
  version: string;
  params_hash: string;
  interval: string;
  label: string;
  validation: string;
  evidence_grade: string;
  trade_from_ms: number;
  warmup_needed_strategy_bars: number;
  warmup_1m_bars_fed: number;
  bars_seen: number;
  warmup_progress: Record<string, PaperWarmup>;
  note: string;
  history_breaks: number;
  watch_proposals: number;
  confirmed_intents: number;
}

export interface PaperPreviousRun {
  state: string;
  reason: string | null;
  awaiting_missing: Record<string, number[]>;
  stopped_at_ms: number;
}

export interface PaperAuditLog {
  ok: boolean;
  reason: string | null;
  warnings: string[];
  repairable_line: number | null;
}

export interface PaperStatusValue {
  state: string;
  reason: string | null;
  enabled: boolean;
  settings_source: string | null;
  strategy: string;
  session_id: string | null;
  next_session_id: string | null;
  observing_since_ms: number | null;
  opened_at_ms: number | null;
  values_as_of_ms: number | null;
  live_bar_since_start: boolean;
  previous_run: PaperPreviousRun | null;
  audit_log: PaperAuditLog | null;
  dropped_updates: number;
  backfill: { ok?: boolean; processed?: number; problems?: string[] } | null;
  reference: PaperReference | null;
  limitations: string[];
  service_state: string | null;
  fault: string | null;
  awaiting_missing: Record<string, number[]> | null;
  last_minute_ms: number | null;
  batches: number | null;
}

export interface ReferenceSignal {
  signal_id: string;
  market: Market;
  symbol: string;
  decision_time_ms: number;
  side: string;
  ref_price: number;
  stop_price: number;
  take_profit_price: number | null;
  valid_until_ms: number;
  label: string;
  validation: string;
}

export interface PaperStatusResponse {
  generated_at: string;
  status: Sourced<PaperStatusValue>;
  reference_signals: Sourced<ReferenceSignal[]> | null;
}

export interface PaperBook {
  equity: string;
  cash: string;
  unrealized_pnl: string;
  open_positions: number;
  positions: Record<string, string>;
  halted: boolean;
  ledger_reconciled: boolean;
}

export interface PaperBookSummary {
  trades: number;
  flagged_trades: number;
  rejections_by_reason: Record<string, number>;
  max_drawdown: string;
  ledger_ok: boolean;
}

export interface PaperBooksValue {
  books: Record<string, PaperBook> | null;
  summary: Record<string, PaperBookSummary> | null;
  ledger_hashes: Record<string, string> | null;
}

export interface PaperBooksResponse {
  generated_at: string;
  books: Sourced<PaperBooksValue>;
}

export interface PaperFill {
  ts_ms: number;
  aggregate_id: string;
  order_id?: string;
  role?: string;
  direction?: string;
  price?: string;
  qty?: string;
  fee?: string;
}

export interface PaperFillsResponse {
  generated_at: string;
  fills: Sourced<PaperFill[]>;
}

export interface PaperTrade {
  trade_id: string;
  book: string;
  market: Market;
  symbol: string;
  side: string;
  signal_id: string | null;
  entry_bar_open_ms: number;
  entry_price: string;
  qty: string;
  exit_bar_open_ms: number;
  exit_price: string;
  exit_reason: string;
  net_pnl: string;
  fees: string;
  r_multiple: string;
  gap_fill: boolean;
  ambiguous_bar: boolean;
  spans_gap: boolean;
  /** Set only when the exit was resolved on backfilled bars while the app was down. */
  resolved_from_backfill?: boolean;
  /** Set only when an entry or exit waited for a candle with trades (ADR-0014 rule 4, sim-v1). */
  deferred_no_trade?: boolean;
  /** Set only when the position was exposed to a registered exchange interruption (backtests, ADR-0013). */
  spans_exchange_downtime?: boolean;
}

export interface PaperRequestRejection {
  ts_ms: number;
  aggregate_id: string;
  /** null for engine-level rejections (invalid request before any book). */
  request_id: string | null;
  reason: string;
}

export interface PaperActivityResponse {
  generated_at: string;
  activity: Sourced<{ trades: PaperTrade[]; request_rejections: PaperRequestRejection[] }>;
}

export interface NewPaperSessionResult {
  new_session_id: string;
  previous_session_id: string | null;
  /** false: an idempotent replay of an already recorded request. */
  created: boolean;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------- backtests (M2-T4)
// /api/backtests: every value is BACKTEST. Fields absent from a run stay absent (never invented).

export interface BacktestSummary {
  run_id: string;
  created_at: string | null;
  strategy_id: string | null;
  strategy_version: string | null;
  params_hash: string | null;
  symbols: string[] | null;
  from: string | null;
  to: string | null;
  segment: string | null;
  evidence_grade: string | null;
  n_trades: number | null;
  win_rate: number | null;
  win_rate_ci: { lower: number; upper: number } | null;
  p_star: number | null;
  margin_lcb: number | null;
  n_trials: number | null;
  recomputed: boolean;
  metrics_verified: boolean | null;
}

export interface BacktestsResponse {
  provenance: Provenance;
  runs: Sourced<BacktestSummary>[];
}

export interface BacktestDetailResponse {
  provenance: Provenance;
  summary: Sourced<BacktestSummary>;
  manifest: Sourced<Record<string, unknown>>;
  metrics: Sourced<Record<string, unknown>>;
}
