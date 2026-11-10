import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ResearchStatus, Sourced } from "../api/types";
import { ResearchStatusView } from "./ResearchStatusBanner";

function status(overrides: Partial<ResearchStatus> = {}): Sourced<ResearchStatus> {
  return {
    value: {
      state: "NO_VALIDATED_STRATEGY",
      validated: 0,
      counts: { REFERENCE: 1, VALIDATED: 0, REJECTED: 0, CANDIDATE: 0, UNREGISTERED: 0 },
      strategies: [
        {
          strategy_id: "bracket_reference",
          tier: "REFERENCE",
          registered: true,
          runs: 0,
          trusted_runs: 0,
          reasons: ["pipeline reference strategy"],
        },
      ],
      runs_recorded: 0,
      runs_trusted: 0,
      preregistration_files: [],
      reasons: ["only pipeline REFERENCE strategies exist (bracket_reference)"],
      statement: "NO VALIDATED STRATEGY: no strategy has passed the preregistered research protocol.",
      ...overrides,
    },
    provenance: "SYSTEM",
    as_of_ms: Date.parse("2026-11-09T12:00:00.000Z"),
    reason: null,
  };
}

describe("research status", () => {
  it("states NO VALIDATED STRATEGY and lists the reference as not validated", () => {
    render(<ResearchStatusView status={status()} />);
    const panel = screen.getByTestId("research-status");
    expect(within(panel).getByText("NO VALIDATED STRATEGY").closest("p")).toHaveClass("bad");
    expect(within(panel).getByTestId("research-tier-bracket_reference")).toHaveTextContent(
      "REFERENCE / NOT VALIDATED",
    );
    expect(within(panel).getByTestId("research-status-reasons")).toHaveTextContent(
      "only pipeline REFERENCE strategies exist",
    );
  });

  it("never renders an unknown status as fine", () => {
    render(
      <ResearchStatusView
        status={{ value: null, provenance: "UNAVAILABLE", as_of_ms: null, reason: "disk error" }}
      />,
    );
    expect(screen.getByTestId("research-status")).toHaveTextContent(
      "Research status unknown: disk error",
    );
    expect(screen.queryByText("NO VALIDATED STRATEGY")).not.toBeInTheDocument();
  });

  it("shows a request error as unknown", () => {
    render(<ResearchStatusView status={undefined} error={new Error("HTTP 500")} />);
    expect(screen.getByTestId("research-status")).toHaveTextContent("Research status unknown");
  });

  it("names a validated strategy by its grade only", () => {
    render(
      <ResearchStatusView
        status={status({
          state: "VALIDATED_STRATEGY_AVAILABLE",
          validated: 1,
          reasons: [],
          statement: "A VALIDATED tier is the research protocol's grade A, not a profitability promise.",
          strategies: [
            {
              strategy_id: "h1_breakout",
              tier: "VALIDATED",
              registered: true,
              runs: 2,
              trusted_runs: 2,
              reasons: ["grade A (credible) on run(s) bt-2"],
            },
          ],
        })}
      />,
    );
    const panel = screen.getByTestId("research-status");
    expect(panel).toHaveTextContent("1 strategy(ies) graded A");
    expect(panel).toHaveTextContent("not a profitability promise");
    expect(within(panel).getByTestId("research-tier-h1_breakout")).toHaveTextContent(
      "VALIDATED (grade A)",
    );
  });

  it("lists walk-forward reports as recorded and never as validation", () => {
    render(
      <ResearchStatusView
        status={status({
          walk_forwards: [
            {
              report: "abcd",
              strategy_id: "bracket_reference",
              reference: true,
              verdict: "INVALID_OOS_EVIDENCE",
              n_trials: 21,
              oos_coverage: 0.25,
              checks_passed: true,
              live_deployable: false,
              not_validation: true,
            },
            { report: "bad", unreadable: "JSONDecodeError" },
          ],
        })}
      />,
    );
    const row = screen.getByTestId("research-wf-abcd");
    expect(within(row).getByText("INVALID_OOS_EVIDENCE")).toBeInTheDocument();
    expect(within(row).getByText("25 %")).toBeInTheDocument();
    expect(within(row).getByText("bracket_reference (REFERENCE)")).toBeInTheDocument();
    expect(screen.getByText(/None of these verdicts is a validation result/)).toBeInTheDocument();
    expect(
      within(screen.getByTestId("research-wf-bad")).getByText("unreadable (JSONDecodeError)"),
    ).toBeInTheDocument();
  });

  it("says when no walk-forward report exists", () => {
    render(<ResearchStatusView status={status()} />);
    expect(screen.getByTestId("research-walkforwards-empty")).toBeInTheDocument();
  });
});
