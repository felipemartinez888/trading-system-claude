import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SignalsResponse } from "../api/types";
import { jsonResponse, sourced } from "../test/fixtures";
import { SignalsPage } from "./SignalsPage";

function renderWith(body: SignalsResponse) {
  vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => jsonResponse(body)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SignalsPage />
    </QueryClientProvider>,
  );
}

describe("Signals inbox", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("lists demo WATCH events with a prominent DEMO badge and watermark", async () => {
    renderWith({
      generated_at: "x",
      signals: sourced(
        [
          {
            signal_id: "01K8",
            seq: 7,
            created_at: "2026-10-20T12:01:00.100Z",
            decision_time: "2026-10-20T12:00:59.999Z",
            market: "usdm",
            symbol: "ETHUSDT",
            timeframe: "1m",
            side: "short",
            stage: "WATCH",
            ref_price: 2500.5,
            move: -0.0042,
            threshold: 0.003,
            strategy_id: "demo_detector",
            evidence_grade: "X",
            run_mode: "LIVE",
            rationale: "DEMO",
          },
        ],
        "DEMO",
      ),
    });
    const table = await screen.findByTestId("signals-table");
    const row = within(table).getAllByRole("row")[1]!;
    expect(within(row).getByRole("img", { name: /provenance DEMO/ })).toHaveClass("prov--hatched");
    expect(row).toHaveTextContent("WATCH");
    expect(row).toHaveTextContent("-0.42%");
    expect(screen.getByTestId("demo-watermark")).toBeInTheDocument();
    expect(screen.getByText(/not a strategy/)).toBeInTheDocument();
  });

  it("explains the empty inbox", async () => {
    renderWith({ generated_at: "x", signals: sourced([], "DEMO") });
    expect(await screen.findByTestId("signals-empty")).toBeInTheDocument();
  });
});
