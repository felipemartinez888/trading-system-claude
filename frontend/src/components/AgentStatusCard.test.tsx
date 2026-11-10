import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AgentStatus, Sourced } from "../api/types";
import { AgentStatusView } from "./AgentStatusCard";

function status(overrides: Partial<AgentStatus> = {}): Sourced<AgentStatus> {
  return {
    value: {
      mode: "off",
      config_ok: true,
      config_errors: [],
      daily_cap_usd: "1.00",
      ledger: { error: null, calls_today: 0, spent_today_usd: "0.00" },
      runtime_wired: false,
      note: "The agent gate is not wired into the paper runtime: no signal is reviewed in any mode yet.",
      ...overrides,
    },
    provenance: "SYSTEM",
    as_of_ms: Date.parse("2026-11-10T08:00:00.000Z"),
    reason: null,
  };
}

describe("agent status", () => {
  it("shows mode OFF, the cap and that the gate is not wired", () => {
    render(<AgentStatusView status={status()} />);
    expect(screen.getByTestId("agent-mode")).toHaveTextContent("Mode: OFF");
    expect(screen.getByTestId("agent-budget")).toHaveTextContent("$0.00 of $1.00 cap");
    expect(screen.getByTestId("agent-not-wired")).toHaveTextContent("not wired");
  });

  it("reports an invalid configuration and an unreadable ledger as problems", () => {
    render(
      <AgentStatusView
        status={status({
          config_ok: false,
          config_errors: ["unknown key"],
          ledger: { error: "cost ledger unreadable (line 3)" },
        })}
      />,
    );
    expect(screen.getByTestId("agent-config-errors")).toHaveClass("bad");
    expect(screen.getByTestId("agent-ledger-error")).toHaveTextContent("every review is refused");
    expect(screen.queryByTestId("agent-budget")).toBeNull();
  });

  it("shows an unavailable status as unknown", () => {
    render(
      <AgentStatusView
        status={{ value: null, provenance: "SYSTEM", as_of_ms: null, reason: "boom" }}
      />,
    );
    expect(screen.getByText("Agent status unknown: boom")).toBeInTheDocument();
  });
});
