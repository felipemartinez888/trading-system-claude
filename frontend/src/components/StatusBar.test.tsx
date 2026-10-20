import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type Mock, beforeEach, describe, expect, it, vi } from "vitest";

import { TOKEN_HEADER } from "../api/client";
import type { LiveLink } from "../api/hooks";
import type { QuitResponse, StatusResponse } from "../api/types";
import { TOKEN, jsonResponse, setToken, statusFixture, system } from "../test/fixtures";
import { StatusBar } from "./StatusBar";

type FetchMock = Mock<typeof fetch>;

const OPEN: LiveLink = { state: "open", shutdown: null };

function renderBar(live: LiveLink = OPEN) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <StatusBar live={live} />
    </QueryClientProvider>,
  );
}

function quitBody(clean: boolean, detail: string | null = null): QuitResponse {
  return {
    accepted: true,
    session: {
      value: { clean, reason: "user_quit", detail },
      provenance: "SYSTEM",
      as_of_ms: Date.now(),
      reason: null,
    },
  };
}

/** A fake backend: GET /api/status returns `current`; POSTs flip it like the real API. */
function installBackend(initial: StatusResponse, quit: QuitResponse = quitBody(true)): FetchMock {
  let current = initial;
  const mock = vi.fn<typeof fetch>(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init?.method ?? "GET";
    if (method === "GET" && url.endsWith("/api/status")) return jsonResponse(current);
    if (method === "POST" && url.endsWith("/api/monitoring/start")) {
      current = statusFixture("RUNNING");
      return jsonResponse(current);
    }
    if (method === "POST" && url.endsWith("/api/monitoring/stop")) {
      current = statusFixture("STOPPED");
      return jsonResponse(current);
    }
    if (method === "POST" && url.endsWith("/api/app/quit")) return jsonResponse(quit, 202);
    return jsonResponse({ detail: "not found" }, 404);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function postCalls(mock: FetchMock) {
  return mock.mock.calls.filter(([, init]) => init?.method === "POST");
}

describe("StatusBar", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows STOPPED from the API with a Start button", async () => {
    installBackend(statusFixture("STOPPED"));
    renderBar();
    expect(await screen.findByTestId("monitoring-state")).toHaveTextContent("STOPPED");
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
  });

  it("shows RUNNING from the API with a Stop button", async () => {
    installBackend(statusFixture("RUNNING"));
    renderBar();
    expect(await screen.findByTestId("monitoring-state")).toHaveTextContent("MONITORING");
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
  });

  it("shows the stream groups' real state from the API", async () => {
    installBackend(statusFixture("RUNNING"));
    renderBar();
    const spot = await screen.findByTestId("stream-spot");
    expect(spot).toHaveTextContent("HEALTHY 6/6");
    expect(spot).toHaveTextContent("SYSTEM");
    expect(spot.querySelector(".stream-ok")).not.toBeNull();
    expect(screen.getByTestId("stream-futures")).toHaveTextContent("HEALTHY 9/9");
  });

  it("never shows a degraded or stopped stream group as healthy", async () => {
    const base = statusFixture("RUNNING");
    installBackend({
      ...base,
      streams: {
        spot: system("DISCONNECTED (4/6 healthy)"),
        futures: {
          value: "HEALTHY 9/9",
          provenance: "STALE",
          as_of_ms: Date.parse("2026-10-20T11:00:00.000Z"),
          reason: "monitoring stopped",
        },
      },
    });
    renderBar();
    const spot = await screen.findByTestId("stream-spot");
    expect(spot).toHaveTextContent("DISCONNECTED (4/6 healthy)");
    expect(spot.querySelector(".stream-bad")).not.toBeNull();
    const futures = screen.getByTestId("stream-futures");
    expect(futures).toHaveTextContent("STALE");
    expect(futures.querySelector(".stream-ok")).toBeNull(); // last-known HEALTHY is not green
  });

  it("before monitoring starts, stream groups are UNAVAILABLE with the reason", async () => {
    installBackend(statusFixture("STOPPED"));
    renderBar();
    const spot = await screen.findByTestId("stream-spot");
    expect(spot).toHaveTextContent("UNAVAILABLE");
    expect(spot).toHaveTextContent("monitoring has not been started");
  });

  it("keeps Stop available while values are STALE but the backend answers (N1)", async () => {
    setToken();
    const base = statusFixture("RUNNING");
    const mock = installBackend({
      ...base,
      heartbeat: {
        value: "2026-10-20T11:59:00.000Z",
        provenance: "STALE",
        as_of_ms: Date.parse("2026-10-20T11:59:00.000Z"),
        reason: null,
      },
    });
    renderBar();
    const stop = await screen.findByRole("button", { name: "Stop" });
    expect(screen.getByTestId("stream-spot")).toHaveTextContent("STALE"); // downgraded display
    expect(stop).toBeEnabled();
    await userEvent.click(stop);
    await waitFor(() => expect(postCalls(mock)).toHaveLength(1));
    expect(String(postCalls(mock)[0]![0])).toBe("/api/monitoring/stop");
  });

  it("shows why a start failed and stays STOPPED", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      monitoring: {
        ...base.monitoring,
        value: {
          state: "STOPPED",
          monitoring_id: null,
          since: null,
          start_allowed: true,
          blocked_reason: null,
          transition: null,
          last_error: "market data failed to start: ConnectError: api.binance.com unreachable",
        },
      },
    });
    renderBar();
    expect(await screen.findByTestId("start-error")).toHaveTextContent("api.binance.com unreachable");
    expect(screen.getByTestId("monitoring-state")).toHaveTextContent("STOPPED");
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled(); // retry allowed
  });

  it("shows a server-side transition and blocks double clicks", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      monitoring: {
        ...base.monitoring,
        value: {
          state: "STOPPED",
          monitoring_id: null,
          since: null,
          start_allowed: false,
          blocked_reason: null,
          transition: "starting",
          last_error: null,
        },
      },
    });
    renderBar();
    expect(await screen.findByRole("button", { name: "Starting…" })).toBeDisabled();
  });

  it("sends the token header on mutations and reflects the new state", async () => {
    setToken();
    const mock = installBackend(statusFixture("STOPPED"));
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(screen.getByTestId("monitoring-state")).toHaveTextContent("MONITORING"),
    );
    const posts = postCalls(mock);
    expect(posts).toHaveLength(1);
    const [url, init] = posts[0]!;
    expect(String(url)).toBe("/api/monitoring/start");
    expect(new Headers(init?.headers).get(TOKEN_HEADER)).toBe(TOKEN);
  });

  it("disables the button while the request is pending", async () => {
    setToken();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const mock = installBackend(statusFixture("STOPPED"));
    const base = mock.getMockImplementation()!;
    mock.mockImplementation(async (input, init) => {
      if (init?.method === "POST") await gate; // hold the request open
      return base(input, init);
    });
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    const pending = await screen.findByRole("button", { name: "Starting…" });
    expect(pending).toBeDisabled();
    release();
    expect(await screen.findByRole("button", { name: "Stop" })).toBeEnabled();
  });

  it("shows a refused transition (423) as an error", async () => {
    setToken();
    const mock = installBackend(statusFixture("STOPPED"));
    const base = mock.getMockImplementation()!;
    mock.mockImplementation(async (input, init) =>
      init?.method === "POST"
        ? jsonResponse({ detail: "event chain verification failed at seq 1: hash mismatch" }, 423)
        : base(input, init),
    );
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText(/Refused \(locked\): event chain verification failed/)).toBeInTheDocument();
    expect(screen.getByTestId("monitoring-state")).toHaveTextContent("STOPPED");
  });

  it("does not send a mutation without a token", async () => {
    const mock = installBackend(statusFixture("STOPPED"));
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText(/no session token/)).toBeInTheDocument();
    expect(postCalls(mock)).toHaveLength(0);
  });

  it("disables Start and explains why when the chain failed verification", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      monitoring: {
        ...base.monitoring,
        value: {
          state: "STOPPED",
          monitoring_id: null,
          since: null,
          start_allowed: false,
          blocked_reason: "event chain verification failed at seq 3: hash mismatch",
          transition: null,
          last_error: null,
        },
      },
      chain: {
        ...base.chain,
        value: {
          ok: false,
          events_checked: 2,
          head_seq: 3,
          head_hash: "b".repeat(64),
          first_bad_seq: 3,
          reason: "hash mismatch",
          verified_at: "2026-10-20T11:00:00.000Z",
        },
      },
    });
    renderBar();
    expect(await screen.findByRole("button", { name: "Start" })).toBeDisabled();
    expect(screen.getByText(/FAILED at seq 3: hash mismatch/)).toBeInTheDocument();
    expect(screen.getByText(/Monitoring is locked/)).toBeInTheDocument();
  });

  it("shows the previous-crash notice with the unobserved interval", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      previous_session: {
        ...base.previous_session,
        value: {
          outcome: "CRASHED",
          session_id: "01K7ZZZZZZZZZZZZZZZZZZZZZZ",
          started_at: "2026-10-20T08:00:00.000Z",
          ended_at: "2026-10-20T09:15:30.000Z",
          end_reason: "crash",
          monitoring_was_active: true,
          unobserved_from: "2026-10-20T09:15:30.000Z",
          unobserved_to: "2026-10-20T11:00:00.000Z",
        },
      },
    });
    renderBar();
    const notice = await screen.findByTestId("crash-notice");
    expect(notice).toHaveTextContent("did not shut down cleanly");
    expect(notice).toHaveTextContent("2026-10-20 09:15:30Z");
    expect(notice).toHaveTextContent("2026-10-20 11:00:00Z");
  });

  it("shows the backend error instead of a state when the API is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    renderBar();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Backend unreachable/);
    expect(screen.queryByTestId("monitoring-state")).not.toBeInTheDocument();
  });
  it("downgrades the chain chip and crash notice to STALE when the backend heartbeat is overdue", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      heartbeat: {
        value: "2026-10-20T11:59:00.000Z",
        provenance: "STALE",
        as_of_ms: Date.parse("2026-10-20T11:59:00.000Z"),
        reason: null,
      },
      previous_session: system({
        outcome: "CRASHED",
        session_id: "01K7ZZZZZZZZZZZZZZZZZZZZZZ",
        started_at: "2026-10-20T08:00:00.000Z",
        ended_at: "2026-10-20T09:15:30.000Z",
        end_reason: "crash",
        monitoring_was_active: false,
        unobserved_from: "2026-10-20T09:15:30.000Z",
        unobserved_to: "2026-10-20T11:00:00.000Z",
      }),
    });
    renderBar();
    expect(await screen.findByTestId("chain-provenance")).toHaveTextContent("STALE");
    expect(screen.getByTestId("crash-provenance")).toHaveTextContent("STALE");
    expect(screen.getByText(/heartbeat is overdue/)).toBeInTheDocument();
    // a stale backend cannot be driven
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("labels the chain chip and crash notice with SYSTEM provenance when fresh", async () => {
    const base = statusFixture("STOPPED");
    installBackend({
      ...base,
      previous_session: system({
        outcome: "CRASHED",
        session_id: "01K7ZZZZZZZZZZZZZZZZZZZZZZ",
        started_at: "2026-10-20T08:00:00.000Z",
        ended_at: "2026-10-20T09:15:30.000Z",
        end_reason: "crash",
        monitoring_was_active: true,
        unobserved_from: "2026-10-20T09:15:30.000Z",
        unobserved_to: "2026-10-20T11:00:00.000Z",
      }),
    });
    renderBar();
    expect(await screen.findByTestId("chain-provenance")).toHaveTextContent("SYSTEM");
    expect(screen.getByTestId("crash-provenance")).toHaveTextContent("SYSTEM");
  });

  it("says the session closed cleanly only when the backend reports it", async () => {
    setToken();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installBackend(statusFixture("RUNNING"), quitBody(true));
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Quit" }));
    expect(await screen.findByTestId("ended-body")).toHaveTextContent(/closed cleanly/);
  });

  it("reports an unclean close truthfully", async () => {
    setToken();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installBackend(statusFixture("RUNNING"), quitBody(false, "session_stopped could not be recorded"));
    renderBar();
    await userEvent.click(await screen.findByRole("button", { name: "Quit" }));
    expect(await screen.findByTestId("ended-title")).toHaveTextContent(/NOT closed cleanly/);
    expect(screen.getByTestId("ended-body")).toHaveTextContent("session_stopped could not be recorded");
    expect(screen.queryByText(/closed cleanly \(/)).not.toBeInTheDocument();
  });

  it("uses the pushed shutdown outcome and never assumes a clean close", () => {
    installBackend(statusFixture("RUNNING"));
    renderBar({ state: "shutdown", shutdown: { clean: false, reason: "signal_SIGTERM", detail: "disk full" } });
    expect(screen.getByTestId("ended-title")).toHaveTextContent(/NOT closed cleanly/);
    cleanup();
    renderBar({ state: "stopping", shutdown: null });
    expect(screen.getByTestId("ended-title")).toHaveTextContent(/shutting down/);
    expect(screen.queryByText(/closed cleanly/)).not.toBeInTheDocument();
    cleanup();
    renderBar({ state: "stopped-unconfirmed", shutdown: null });
    expect(screen.getByTestId("ended-body")).toHaveTextContent(/could not be confirmed/);
  });
});
