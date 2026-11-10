import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PaperActivityResponse,
  PaperBooksResponse,
  PaperFillsResponse,
  PaperStatusResponse,
  PaperStatusValue,
} from "../api/types";
import { jsonResponse, setToken, sourced, system } from "../test/fixtures";
import { PaperPage } from "./PaperPage";

const LIMITATION = "Restart equivalence is not guaranteed. (fixture text from the backend)";

function statusValue(overrides: Partial<PaperStatusValue> = {}): PaperStatusValue {
  return {
    state: "RUNNING",
    reason: null,
    enabled: true,
    settings_source: "configs/paper.yaml",
    strategy: "bracket_reference",
    session_id: "paper",
    next_session_id: "paper",
    observing_since_ms: 1_760_000_000_000,
    opened_at_ms: 1_760_000_000_000,
    values_as_of_ms: 1_760_000_059_999,
    live_bar_since_start: true,
    previous_run: null,
    audit_log: { ok: true, reason: null, warnings: [], repairable_line: null },
    dropped_updates: 0,
    backfill: null,
    reference: {
      id: "bracket_reference",
      version: "1",
      params_hash: "abc",
      interval: "15m",
      label: "REFERENCE",
      validation: "NOT_VALIDATED",
      evidence_grade: "X",
      trade_from_ms: 1_760_000_000_000,
      warmup_needed_strategy_bars: 741,
      warmup_1m_bars_fed: 22_000,
      bars_seen: 10,
      warmup_progress: {
        "spot:BTCUSDT": {
          strategy_bars: 12,
          needed: 741,
          ready: false,
          history_breaks: 1,
          last_break_detected_at_ms: 1_760_000_000_000,
          last_break_phase: "live",
          last_break_reason: "an incomplete 15m bucket restarted this instrument's history",
        },
        "usdm:BTCUSDT": { strategy_bars: 748, needed: 741, ready: true },
      },
      note: "needs about 741 contiguous 15m bars",
      history_breaks: 1,
      watch_proposals: 7,
      confirmed_intents: 1,
    },
    limitations: [LIMITATION],
    service_state: "RUNNING",
    fault: null,
    awaiting_missing: {},
    last_minute_ms: 1_760_000_000_000,
    batches: 12,
    ...overrides,
  };
}

const BOOK = {
  equity: "10000.00000000",
  cash: "9000.00000000",
  unrealized_pnl: "1.50000000",
  open_positions: 1,
  positions: { "spot:BTCUSDT": "0.01000000" },
  halted: false,
  ledger_reconciled: true,
};
const SUMMARY = {
  trades: 2,
  flagged_trades: 0,
  rejections_by_reason: { "risk_blocked:stale_data": 3 },
  max_drawdown: "0.0100",
  ledger_ok: true,
};

interface Bodies {
  status: PaperStatusResponse;
  books?: PaperBooksResponse;
  fills?: PaperFillsResponse;
  activity?: PaperActivityResponse;
  sessionNew?: Response;
}

function renderWith(bodies: Bodies) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith("/api/paper/status")) return jsonResponse(bodies.status);
    if (url.endsWith("/api/paper/books")) {
      return jsonResponse(bodies.books ?? { generated_at: "x", books: sourced(null, "UNAVAILABLE", "no paper session") });
    }
    if (url.endsWith("/api/paper/fills")) {
      return jsonResponse(bodies.fills ?? { generated_at: "x", fills: sourced(null, "UNAVAILABLE", "no paper session") });
    }
    if (url.endsWith("/api/paper/activity")) {
      return jsonResponse(bodies.activity ?? { generated_at: "x", activity: sourced(null, "UNAVAILABLE", "no paper session") });
    }
    if (url.endsWith("/api/paper/session/new")) {
      return bodies.sessionNew ?? jsonResponse({ new_session_id: "paper-2", previous_session_id: "paper", created: true });
    }
    return jsonResponse({ detail: "unexpected" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PaperPage />
    </QueryClientProvider>,
  );
  return fetchMock;
}

function running(): Bodies {
  return {
    status: {
      generated_at: "x",
      status: system(statusValue()),
      reference_signals: sourced(
        [
          {
            signal_id: "01SIG",
            market: "usdm",
            symbol: "BTCUSDT",
            decision_time_ms: 1_760_000_899_999,
            side: "long",
            ref_price: 100,
            stop_price: 99,
            take_profit_price: 102,
            valid_until_ms: 1_760_001_799_999,
            label: "REFERENCE",
            validation: "NOT_VALIDATED",
          },
        ],
        "SIMULATED",
      ),
    },
    books: {
      generated_at: "x",
      books: sourced(
        {
          books: { baseline: BOOK, shadow: BOOK, benchmark: BOOK },
          summary: { baseline: SUMMARY, shadow: SUMMARY, benchmark: SUMMARY },
          ledger_hashes: null,
        },
        "STALE",
        "last processed bar closed 300 s ago",
      ),
    },
    activity: {
      generated_at: "x",
      activity: sourced(
        {
          trades: [
            {
              trade_id: "shadow:ref:1",
              book: "shadow",
              market: "usdm",
              symbol: "BTCUSDT",
              side: "long",
              signal_id: "01SIG",
              entry_bar_open_ms: 1_760_000_900_000,
              entry_price: "100.1",
              qty: "0.01",
              exit_bar_open_ms: 1_760_001_000_000,
              exit_price: "99.0",
              exit_reason: "stop",
              net_pnl: "-0.0112",
              fees: "0.001",
              r_multiple: "-1.02",
              gap_fill: false,
              ambiguous_bar: false,
              spans_gap: true,
              deferred_no_trade: true,
            },
          ],
          request_rejections: [
            { ts_ms: 1_760_000_900_000, aggregate_id: "baseline", request_id: "ref:usdm:BTCUSDT:1", reason: "risk_blocked:no_market_view" },
          ],
        },
        "SIMULATED",
      ),
    },
  };
}

describe("Paper page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows a disabled runtime with its reason, no book values and the limitations", async () => {
    renderWith({
      status: {
        generated_at: "x",
        status: system(statusValue({ state: "DISABLED", reason: "paper is off (configs/paper.yaml)", reference: null, strategy: "none", service_state: null })),
        reference_signals: null,
      },
    });
    expect(await screen.findByTestId("paper-state")).toHaveTextContent("DISABLED");
    expect(screen.getByTestId("paper-reason")).toHaveTextContent("paper is off");
    expect(await screen.findByText(/Book values: not available — no paper session/)).toBeInTheDocument();
    const limits = screen.getByTestId("paper-limitations");
    expect(limits).toHaveTextContent("Restart equivalence is not guaranteed");
    expect(screen.getByTestId("paper-mvp-note")).toHaveTextContent("not evidence of M1–M4 acceptance");
    expect(screen.getByTestId("paper-reference-none")).toBeInTheDocument();
  });

  it("says why a selected strategy has not started instead of 'no strategy selected' (N-1/N-2)", async () => {
    const cases: [Partial<PaperStatusValue>, RegExp, RegExp][] = [
      [{ observation: null }, /waiting for the first market-data poll/, /not established/],
      [
        {
          observation: {
            state: "blocked",
            attempted_at_ms: 1_760_000_001_500,
            observing_from_ms: null,
            code: "clock_discontinuity",
            problem: "the wall clock moved -300 ms against the monotonic clock since the server clock round",
          },
        },
        /observation is blocked: the wall clock moved -300 ms/,
        /blocked — the wall clock moved -300 ms/,
      ],
      [
        {
          observation: {
            state: "active",
            attempted_at_ms: 1_760_000_001_500,
            observing_from_ms: 1_760_000_040_000,
            code: null,
            problem: null,
            offset_ms: 3,
            max_rtt_ms: 40,
            uncertainty_ms: 23,
            allowance_ms: 2,
          },
        },
        /waiting for its first observed minute \(from 2025-10-09/,
        /from 2025-10-09.*offset 3 ms, max round trip 40 ms, allowance 2 ms/,
      ],
      [{ state: "AWAITING_BACKFILL", observation: null }, /paper is AWAITING_BACKFILL/, /not established/],
    ];
    for (const [overrides, card, row] of cases) {
      renderWith({
        status: { generated_at: "x", status: system(statusValue({ reference: null, ...overrides })), reference_signals: null },
      });
      const deferred = await screen.findByTestId("paper-reference-deferred");
      expect(deferred).toHaveTextContent("bracket_reference is selected and not started yet");
      expect(deferred).toHaveTextContent(card);
      expect(screen.queryByTestId("paper-reference-none")).toBeNull();
      expect(screen.queryByText(/No live strategy is selected/)).toBeNull();
      expect(screen.getByTestId("paper-runtime")).toHaveTextContent(row);
      cleanup();
    }
  });

  it("shows the backend's actionable hint for a blocked observation, and nothing when absent", async () => {
    const hint =
      "The computer clock differs from Binance server time by more than the allowed limit. " +
      "Until then every bar is processed unobserved: no entry is created or filled (fail closed).";
    renderWith({
      status: {
        generated_at: "x",
        status: system(statusValue({ reference: null, hint })),
        reference_signals: null,
      },
    });
    expect(await screen.findByTestId("paper-hint")).toHaveTextContent(`What to do: ${hint}`);
    cleanup();
    renderWith({
      status: { generated_at: "x", status: system(statusValue({ reference: null })), reference_signals: null },
    });
    expect(await screen.findByTestId("paper-runtime")).toBeInTheDocument();
    expect(screen.queryByTestId("paper-hint")).toBeNull();
  });

  it("still renders the deferred strategy for a backend that does not report the observation", async () => {
    const { observation: _, ...old } = statusValue({ reference: null });
    renderWith({ status: { generated_at: "x", status: system(old as PaperStatusValue), reference_signals: null } });
    const deferred = await screen.findByTestId("paper-reference-deferred");
    expect(deferred).toHaveTextContent("waiting for the first market-data poll");
  });

  it("labels books, trades, rejections and the reference strategy honestly", async () => {
    renderWith(running());
    const books = await screen.findByTestId("book-shadow");
    expect(books).toHaveTextContent("9000.00000000");
    expect(screen.getByTestId("paper-books")).toContainElement(
      screen.getAllByRole("img", { name: /provenance STALE.*300 s ago/ })[0]!,
    );
    const trades = await screen.findByTestId("paper-trades");
    expect(trades).toHaveTextContent("stop");
    expect(trades).toHaveTextContent("spans data gap");
    expect(trades).toHaveTextContent("waited for a traded candle");
    expect(screen.getByTestId("paper-request-rejections")).toHaveTextContent("risk_blocked:no_market_view");
    expect(screen.getByTestId("paper-fill-rejections")).toHaveTextContent("risk_blocked:stale_data");

    const reference = screen.getByTestId("paper-reference");
    expect(within(reference).getAllByRole("img", { name: /validation REFERENCE \/ NOT VALIDATED/ }).length).toBeGreaterThan(0);
    expect(within(reference).queryByText(/INVALID/)).toBeNull(); // grade X is not shown as a data verdict
    expect(reference).toHaveTextContent("WATCH proposals (counted, never traded)");
    const signals = within(reference).getByTestId("paper-signals");
    expect(signals).not.toHaveTextContent("WATCH");
    expect(within(signals).getAllByRole("row")).toHaveLength(2);
    const warmup = within(reference).getByTestId("paper-warmup");
    expect(warmup).toHaveTextContent("warming up");
    expect(warmup).toHaveTextContent("incomplete 15m bucket");
    expect(warmup).toHaveTextContent("not available"); // usdm row: backend without B-3 fields
  });

  it("keeps a failure and the previous run visible", async () => {
    renderWith({
      status: {
        generated_at: "x",
        status: system(
          statusValue({
            state: "SUBSCRIPTION_LOST",
            reason: "market-data subscription closed",
            previous_run: { state: "AWAITING_BACKFILL", reason: "downtime unresolved", awaiting_missing: {}, stopped_at_ms: 1_760_000_000_000 },
          }),
        ),
        reference_signals: null,
      },
    });
    const state = await screen.findByTestId("paper-state");
    expect(state).toHaveClass("chip--bad");
    expect(screen.getByTestId("paper-reason")).toHaveClass("bad");
    expect(screen.getByTestId("paper-previous-run")).toHaveTextContent("AWAITING_BACKFILL");
  });

  it("offers only the new-session action, token-protected, and shows refusals verbatim", async () => {
    const user = userEvent.setup();
    const bodies = running();
    bodies.sessionNew = jsonResponse({ detail: "paper is processing; stop monitoring first" }, 409);
    setToken();
    const fetchMock = renderWith(bodies);
    const button = await screen.findByRole("button", { name: "Record new paper session" });
    expect(button).toBeDisabled(); // a note is required
    expect(screen.queryByRole("button", { name: /reset|recover|order|kill/i })).toBeNull();
    await user.type(screen.getByRole("textbox"), "fresh start after config change");
    expect(button).toBeEnabled();
    await user.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("paper is processing; stop monitoring first");
    const post = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/api/paper/session/new"));
    expect(post).toBeDefined();
    const init = post![1]!;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({ note: "fresh start after config change" });
  });

  it("disables the action without a session token", async () => {
    renderWith(running());
    const button = await screen.findByRole("button", { name: "Record new paper session" });
    await userEvent.setup().type(screen.getByRole("textbox"), "note");
    expect(button).toBeDisabled();
    expect(screen.getByText(/Open the dashboard from the app URL/)).toBeInTheDocument();
  });

  it("never states an empty or missing fact while loading or after a failed request", async () => {
    const bodies = running();
    const fetchMock = renderWith(bodies);
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/paper/status")) return jsonResponse(bodies.status);
      return jsonResponse({ detail: "boom" }, 500);
    });
    expect(await screen.findByText(/Book values could not be loaded/)).toBeInTheDocument();
    expect(await screen.findByText(/Fills could not be loaded/)).toBeInTheDocument();
    expect(await screen.findByText(/Activity could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/no paper session/)).toBeNull();
    expect(screen.queryByText(/No fills/)).toBeNull();
    expect(screen.queryByText(/No closed trades/)).toBeNull();
  });

  it("keeps the MVP note visible when paper status is unavailable", async () => {
    renderWith({
      status: {
        generated_at: "x",
        status: sourced<PaperStatusValue>(null, "UNAVAILABLE", "paper trading is not configured"),
        reference_signals: null,
      },
    });
    expect(await screen.findByTestId("paper-unavailable")).toHaveTextContent("paper trading is not configured");
    expect(screen.getByTestId("paper-mvp-note")).toBeInTheDocument();
  });

  it("reports what the backend recorded after a new-session request", async () => {
    const user = userEvent.setup();
    setToken();
    renderWith(running());
    await user.type(await screen.findByRole("textbox"), "new config");
    await user.click(screen.getByRole("button", { name: "Record new paper session" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Recorded session paper-2");
  });
});
