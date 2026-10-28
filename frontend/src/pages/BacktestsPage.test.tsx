import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BacktestDetailResponse, BacktestSummary, BacktestsResponse } from "../api/types";
import { jsonResponse, sourced } from "../test/fixtures";
import { BacktestsPage } from "./BacktestsPage";

function run(overrides: Partial<BacktestSummary>): BacktestSummary {
  return {
    run_id: "bt-1",
    created_at: "2026-10-20T12:00:00.000Z",
    strategy_id: "h1_breakout",
    strategy_version: "1",
    params_hash: "0123456789abcdef0123",
    symbols: ["spot:BTCUSDT"],
    from: "2024-01-01T00:00:00Z",
    to: "2025-01-01T00:00:00Z",
    segment: "development",
    evidence_grade: "C",
    n_trades: 40,
    win_rate: 0.55,
    win_rate_ci: { lower: 0.4, upper: 0.7 },
    p_star: 0.5,
    margin_lcb: -0.1,
    n_trials: 3,
    recomputed: false,
    metrics_verified: true,
    ...overrides,
  };
}

function renderWith(list: BacktestsResponse, detail?: BacktestDetailResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/backtests")) return jsonResponse(list);
      if (detail !== undefined && url.includes("/api/backtests/")) return jsonResponse(detail);
      return jsonResponse({ detail: "unknown run" }, 404);
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BacktestsPage />
    </QueryClientProvider>,
  );
}

describe("Backtests page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("explains that no run exists and that runs are trials started deliberately", async () => {
    renderWith({ provenance: "BACKTEST", runs: [] });
    const empty = await screen.findByTestId("backtests-empty");
    expect(empty).toHaveTextContent("No backtest runs recorded yet");
    expect(empty).toHaveTextContent("statistical trial");
  });

  it("separates reference runs and never shows them as validated or invalid data", async () => {
    renderWith({
      provenance: "BACKTEST",
      runs: [
        sourced(run({ run_id: "bt-research" }), "BACKTEST"),
        sourced(run({ run_id: "bt-ref", strategy_id: "bracket_reference", evidence_grade: "X" }), "BACKTEST"),
      ],
    });
    const research = await screen.findByTestId("research-runs");
    expect(research).toHaveTextContent("bt-research");
    expect(research).not.toHaveTextContent("bt-ref");
    expect(within(research).getByRole("img", { name: /validation INSUFFICIENT \(C – insufficient\)/ })).toBeInTheDocument();
    const reference = screen.getByTestId("reference-runs");
    expect(reference).toHaveTextContent("bt-ref");
    expect(within(reference).getByRole("img", { name: /validation REFERENCE \/ NOT VALIDATED/ })).toBeInTheDocument();
    expect(reference).not.toHaveTextContent("INVALID");
    expect(research).toHaveTextContent("55.00% [40.00%, 70.00%]");
  });

  it("shows run details as recorded, with unavailable fields and an unverified-metrics warning", async () => {
    const summary = run({ run_id: "bt-x", metrics_verified: false, win_rate: null, win_rate_ci: null });
    renderWith(
      { provenance: "BACKTEST", runs: [sourced(summary, "BACKTEST")] },
      {
        provenance: "BACKTEST",
        summary: sourced(summary, "BACKTEST"),
        manifest: sourced(
          {
            datasets: { bars: ["ds1"] },
            cost_model: { version: "cm-1" },
            code: "git:abc1234",
            runner_version: "r1",
            sim_engine_version: "s1",
            features_version: "f1",
            config_hash: "cfg123",
            seed: 7,
            seed_source: "trial_key",
            hypothesis_id: "H1",
            prereg_id: null,
            manifest_hash: "man456",
          },
          "BACKTEST",
        ),
        metrics: sourced({ trades: { n: 40, expectancy_r: 0.0123 }, grade_reasons: ["insufficient: wide CI"] }, "BACKTEST"),
      },
    );
    await userEvent.setup().click(await screen.findByRole("button", { name: "bt-x" }));
    const detail = await screen.findByTestId("backtest-detail");
    expect(within(detail).getByTestId("metrics-unverified")).toBeInTheDocument();
    expect(detail).toHaveTextContent("0.0123");
    expect(detail).toHaveTextContent("ds1");
    expect(detail).toHaveTextContent("insufficient: wide CI");
    // real manifest keys are shown, not reported missing
    expect(detail).toHaveTextContent("git:abc1234");
    expect(detail).toHaveTextContent("r1 / s1 / f1");
    expect(detail).toHaveTextContent("7 (trial_key)");
    expect(detail).toHaveTextContent("man456");
    expect(within(detail).getByRole("img", { name: /validation UNVERIFIED/ })).toBeInTheDocument();
    // nothing invented: fields the run does not contain stay unavailable
    expect(within(detail).getAllByText("not available").length).toBeGreaterThan(3);
  });

  it("shows why metrics are unavailable", async () => {
    const summary = run({ run_id: "bt-m" });
    renderWith(
      { provenance: "BACKTEST", runs: [sourced(summary, "BACKTEST")] },
      {
        provenance: "BACKTEST",
        summary: sourced(summary, "BACKTEST"),
        manifest: sourced({}, "BACKTEST"),
        metrics: sourced<Record<string, unknown>>(null, "UNAVAILABLE", "metrics.json missing"),
      },
    );
    await userEvent.setup().click(await screen.findByRole("button", { name: "bt-m" }));
    expect(await screen.findByText(/Metrics unavailable: metrics.json missing/)).toBeInTheDocument();
  });

  it("does not show a grade from unverified metrics as validated", async () => {
    renderWith({
      provenance: "BACKTEST",
      runs: [sourced(run({ run_id: "bt-a", evidence_grade: "A", metrics_verified: false }), "BACKTEST")],
    });
    const research = await screen.findByTestId("research-runs");
    expect(within(research).getByRole("img", { name: /UNVERIFIED \(recorded grade A\)/ })).toBeInTheDocument();
    expect(research).not.toHaveTextContent("VALIDATED (A");
  });
});
