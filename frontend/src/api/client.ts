// Thin fetch client for the local API. Mutations carry the per-launch token that the backend
// injected into index.html as <meta name="tsc-token">; the browser adds the Origin header itself.

import type {
  BacktestDetailResponse,
  BacktestsResponse,
  BarsResponse,
  CoverageResponse,
  EventsResponse,
  GapsResponse,
  Market,
  MarketsResponse,
  NewPaperSessionResult,
  NotificationsResponse,
  PaperActivityResponse,
  PaperBooksResponse,
  PaperFillsResponse,
  PaperStatusResponse,
  QuitResponse,
  ResearchStatusResponse,
  SignalsResponse,
  StatusResponse,
  StreamsHealthResponse,
} from "./types";

export const TOKEN_HEADER = "X-TSC-Token";
export const TOKEN_META = "tsc-token";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** The per-launch token, or null when the page was not served by the backend (e.g. `pnpm dev`). */
export function readToken(doc: Document = document): string | null {
  const meta = doc.querySelector<HTMLMetaElement>(`meta[name="${TOKEN_META}"]`);
  const token = meta?.content.trim();
  return token ? token : null;
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText || "request failed";
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        detail = typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { headers: { Accept: "application/json" }, cache: "no-store" };
  if (signal) init.signal = signal;
  return parse<T>(await fetch(path, init));
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const token = readToken();
  if (token === null) {
    // Fail closed: without the token the backend would answer 403 anyway; say why up front.
    throw new ApiError(403, "no session token in this page; open the dashboard from the app URL");
  }
  const headers: Record<string, string> = { Accept: "application/json", [TOKEN_HEADER]: token };
  const init: RequestInit = { method: "POST", headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return parse<T>(await fetch(path, init));
}

export const api = {
  status: (signal?: AbortSignal) => get<StatusResponse>("/api/status", signal),
  coverage: (signal?: AbortSignal) => get<CoverageResponse>("/api/coverage", signal),
  events: (limit = 200, signal?: AbortSignal) =>
    get<EventsResponse>(`/api/events?limit=${encodeURIComponent(String(limit))}`, signal),
  markets: (signal?: AbortSignal) => get<MarketsResponse>("/api/markets", signal),
  streamsHealth: (signal?: AbortSignal) =>
    get<StreamsHealthResponse>("/api/health/streams", signal),
  gaps: (signal?: AbortSignal) => get<GapsResponse>("/api/gaps", signal),
  bars: (market: Market, symbol: string, fromIso: string, toIso: string, signal?: AbortSignal) =>
    get<BarsResponse>(
      `/api/bars?${new URLSearchParams({ market, symbol, interval: "1m", from: fromIso, to: toIso }).toString()}`,
      signal,
    ),
  signals: (signal?: AbortSignal) => get<SignalsResponse>("/api/signals", signal),
  notifications: (signal?: AbortSignal) =>
    get<NotificationsResponse>("/api/notifications", signal),
  startMonitoring: () => post<StatusResponse>("/api/monitoring/start"),
  stopMonitoring: () => post<StatusResponse>("/api/monitoring/stop"),
  quit: () => post<QuitResponse>("/api/app/quit"),
  paperStatus: (signal?: AbortSignal) => get<PaperStatusResponse>("/api/paper/status", signal),
  paperBooks: (signal?: AbortSignal) => get<PaperBooksResponse>("/api/paper/books", signal),
  paperFills: (signal?: AbortSignal) => get<PaperFillsResponse>("/api/paper/fills", signal),
  paperActivity: (signal?: AbortSignal) =>
    get<PaperActivityResponse>("/api/paper/activity", signal),
  /** The only paper operator action: record a new session that opens at the next start. */
  newPaperSession: (note: string, requestId: string) =>
    post<NewPaperSessionResult>("/api/paper/session/new", { note, request_id: requestId }),
  backtests: (signal?: AbortSignal) => get<BacktestsResponse>("/api/backtests", signal),
  researchStatus: (signal?: AbortSignal) =>
    get<ResearchStatusResponse>("/api/research/status", signal),
  backtest: (runId: string, signal?: AbortSignal) =>
    get<BacktestDetailResponse>(`/api/backtests/${encodeURIComponent(runId)}`, signal),
};

export function liveUrl(token: string, loc: Location = window.location): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/ws/live?token=${encodeURIComponent(token)}`;
}
