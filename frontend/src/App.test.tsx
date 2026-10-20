import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { CoverageResponse, EventsResponse } from "./api/types";
import { ALL_PAGES, WORKSPACES } from "./navigation";
import { jsonResponse, statusFixture } from "./test/fixtures";

const AS_OF = Date.parse("2026-10-20T12:00:00.000Z");

const coverage: CoverageResponse = {
  generated_at: "2026-10-20T12:00:00.000Z",
  intervals: {
    value: [
      {
        monitoring_id: "M1",
        start: "2026-10-20T10:00:00.000Z",
        end: "2026-10-20T10:30:00.000Z",
        end_reason: "user",
      },
    ],
    provenance: "SYSTEM",
    as_of_ms: AS_OF,
    reason: null,
  },
  gaps: {
    value: [{ start: "2026-10-20T10:30:00.000Z", end: null, after_reason: "user" }],
    provenance: "SYSTEM",
    as_of_ms: AS_OF,
    reason: null,
  },
};

const events: EventsResponse = {
  generated_at: "2026-10-20T12:00:00.000Z",
  events: {
    value: {
      items: [
        {
          seq: 1,
          ts: "2026-10-20T10:00:00.000Z",
          type: "session_started",
          aggregate_type: "session",
          aggregate_id: "01K80000000000000000000000",
          payload: { session_id: "01K80000000000000000000000" },
          prev_hash: "0".repeat(64),
          hash: "c".repeat(64),
        },
      ],
      head_seq: 1,
      has_more: false,
    },
    provenance: "SYSTEM",
    as_of_ms: AS_OF,
    reason: null,
  },
  chain: statusFixture("STOPPED").chain,
};

function renderApp(path: string) {
  window.history.replaceState(null, "", path);
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/status")) return jsonResponse(statusFixture("STOPPED"));
      if (url.startsWith("/api/coverage")) return jsonResponse(coverage);
      if (url.startsWith("/api/events")) return jsonResponse(events);
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

describe("App shell", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("lists the five workspaces and all their pages in the left rail", () => {
    renderApp("/system/health");
    const rail = screen.getByRole("navigation", { name: "Workspaces" });
    expect(WORKSPACES.map((w) => w.title)).toEqual([
      "Live",
      "Paper",
      "Research",
      "Performance",
      "System",
    ]);
    for (const page of ALL_PAGES) {
      expect(within(rail).getByRole("link", { name: new RegExp(page.title) })).toHaveAttribute(
        "href",
        page.path,
      );
    }
  });

  it.each(ALL_PAGES.filter((p) => !p.implemented))(
    "renders a meaningful empty state for $path",
    (page) => {
      renderApp(page.path);
      expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
      expect(screen.getByText(page.willShow)).toBeInTheDocument();
      expect(screen.getByText(page.arrives)).toBeInTheDocument();
    },
  );

  it("shows observed vs unobserved coverage on System › Health", async () => {
    renderApp("/system/health");
    const table = await screen.findByTestId("coverage-table");
    expect(within(table).getByText("observed")).toBeInTheDocument();
    expect(within(table).getByText("not observed")).toBeInTheDocument();
    expect(within(table).getByText("not observing now")).toBeInTheDocument();
  });

  it("lists recent events on System › Logs & audit", async () => {
    renderApp("/system/logs");
    const table = await screen.findByTestId("events-table");
    expect(within(table).getByText("session_started")).toBeInTheDocument();
  });

  it("navigates between pages without reloading", async () => {
    renderApp("/system/health");
    await userEvent.click(screen.getByRole("link", { name: /Backtests/ }));
    expect(window.location.pathname).toBe("/research/backtests");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Research › Backtests");
  });
});
