import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MarketsResponse, Provenance } from "../api/types";
import { candleOf, gapMinutes } from "../components/CandleChart";
import { jsonResponse, marketRow, sourced, statusFixture } from "../test/fixtures";
import { MarketsPage, MarketsTable } from "./MarketsPage";

function withClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function backend(markets: MarketsResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/status")) return jsonResponse(statusFixture("RUNNING"));
      if (url.startsWith("/api/markets")) return jsonResponse(markets);
      if (url.startsWith("/api/bars"))
        return jsonResponse({
          generated_at: "2026-10-20T12:00:00.000Z",
          market: "spot",
          symbol: "BTCUSDT",
          interval: "1m",
          from_ms: 0,
          to_ms: 1,
          bars: sourced([], "HISTORICAL"),
          truncated: false,
        });
      if (url.startsWith("/api/gaps"))
        return jsonResponse({ generated_at: "x", gaps: sourced([], "SYSTEM") });
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
}

const ALL: Provenance[] = [
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
];

describe("Markets", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it.each(ALL)("renders %s values with their badge, exactly as the API labels them", (prov) => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => jsonResponse(statusFixture("RUNNING"))));
    withClient(
      <MarketsTable rows={[marketRow("BTCUSDT", prov)]} selected={null} onSelect={() => undefined} />,
    );
    const row = screen.getByTestId("market-row-spot:BTCUSDT");
    for (const id of ["last-close", "book", "mark", "funding", "change-24h"]) {
      const cell = within(row).getByTestId(id);
      expect(cell).toHaveAttribute("data-provenance", prov);
      if (prov === "UNAVAILABLE") expect(cell).toHaveTextContent("—");
    }
    if (prov === "DEMO") {
      expect(within(row).getByTestId("book").querySelector(".prov--hatched")).not.toBeNull();
    }
  });

  it("shows the forming candle as PARTIAL and the exchange status as HISTORICAL", () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => jsonResponse(statusFixture("RUNNING"))));
    withClient(
      <MarketsTable rows={[marketRow("ETHUSDT", "LIVE")]} selected={null} onSelect={() => undefined} />,
    );
    const row = screen.getByTestId("market-row-spot:ETHUSDT");
    expect(within(row).getByTestId("partial")).toHaveAttribute("data-provenance", "PARTIAL");
    expect(within(row).getByTestId("status")).toHaveAttribute("data-provenance", "HISTORICAL");
    expect(within(row).getByTestId("spread")).toHaveTextContent("9.95 bp");
  });

  it("explains the empty state before monitoring starts", async () => {
    backend({ generated_at: "x", running: sourced(false, "SYSTEM"), rows: [] });
    withClient(<MarketsPage />);
    expect(await screen.findByTestId("markets-empty")).toHaveTextContent(/Press Start/);
  });

  it("labels the page STALE when monitoring is stopped and draws the chart", async () => {
    backend({
      generated_at: "x",
      running: sourced(false, "SYSTEM"),
      rows: [marketRow("BTCUSDT", "STALE")],
    });
    withClient(<MarketsPage />);
    expect(await screen.findByText(/last known ones \(STALE\)/)).toBeInTheDocument();
    expect(screen.getByTestId("candle-chart")).toBeInTheDocument();
    expect(screen.getByText(/TradingView/)).toBeInTheDocument(); // required attribution
  });

  it("says when there are no bars to draw", async () => {
    backend({
      generated_at: "x",
      running: sourced(true, "SYSTEM"),
      rows: [marketRow("BTCUSDT", "UNAVAILABLE")],
    });
    withClient(<MarketsPage />);
    expect(await screen.findByTestId("chart-empty")).toBeInTheDocument();
  });

  it("colours candles by source and shades only missing minutes", () => {
    const bar = {
      open_time_ms: 0,
      close_time_ms: 59_999,
      open: 1,
      high: 2,
      low: 0.5,
      close: 1.5,
      volume: 1,
      observed_session_id: null,
      provenance: "HISTORICAL" as const,
    };
    expect(candleOf({ ...bar, source: "ws_live" }).color).toBeUndefined();
    expect(candleOf({ ...bar, source: "rest_recovered" }).color).toBe("#cc79a7");
    expect(candleOf({ ...bar, source: "rest_backfill" }).color).toBe("#8a93a6");
    const gap = {
      gap_id: "G",
      market: "spot" as const,
      symbol: "BTCUSDT",
      interval: "1m",
      start: "",
      end: "",
      from_ms: 0,
      to_ms: 300_000,
      minutes: 5,
      missing_minutes: 2,
      missing_ranges_ms: [[120_000, 240_000]] as [number, number][],
      cause: "disconnect",
      detected_at: "",
      resolution: "pending",
      resolved_at: null,
    };
    expect(gapMinutes([gap], "spot", "BTCUSDT", 0, 600_000)).toEqual([120_000, 180_000]);
    expect(gapMinutes([gap], "usdm", "BTCUSDT", 0, 600_000)).toEqual([]);
  });
});
