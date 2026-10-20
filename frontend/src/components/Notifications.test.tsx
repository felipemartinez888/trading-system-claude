import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { applyDelta, queryKeys } from "../api/hooks";
import type { MarketsResponse, NotificationOut, NotificationsResponse } from "../api/types";
import { jsonResponse, marketRow, sourced } from "../test/fixtures";
import { NotificationBell, isAttention } from "./Notifications";

function note(id: string, over: Partial<NotificationOut> = {}): NotificationOut {
  return {
    id,
    seq: Number(id.slice(1)),
    ts: `2026-10-20T12:00:0${id.slice(1)}.000Z`,
    kind: "stream_state_changed",
    severity: "warning",
    title: `note ${id}`,
    detail: "",
    market: "spot",
    symbol: "BTCUSDT",
    provenance: "SYSTEM",
    ...over,
  };
}

function feed(items: NotificationOut[]): NotificationsResponse {
  return { generated_at: "x", notifications: sourced(items, "SYSTEM"), head_seq: 9 };
}

function renderBell(items: NotificationOut[]) {
  vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => jsonResponse(feed(items))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <NotificationBell />
    </QueryClientProvider>,
  );
  return client;
}

describe("notifications", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("counts warnings, errors and demo WATCH as unread; info is not", () => {
    expect(isAttention(note("e1"))).toBe(true);
    expect(isAttention(note("e2", { severity: "info" }))).toBe(false);
    expect(isAttention(note("e3", { severity: "info", kind: "signal_created" }))).toBe(true);
  });

  it("shows a badge, lists items with provenance and marks them read on open", async () => {
    renderBell([
      note("e3", { kind: "signal_created", severity: "info", provenance: "DEMO", title: "DEMO WATCH" }),
      note("e2", { severity: "info", title: "HEALTHY again" }),
      note("e1", { title: "kline STALE" }),
    ]);
    expect(await screen.findByTestId("notification-badge")).toHaveTextContent("2");
    await userEvent.click(screen.getByRole("button", { name: /Notifications, 2 unread/ }));
    const list = screen.getByTestId("inbox-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByText("DEMO WATCH").parentElement?.querySelector(".prov--demo")).not.toBeNull();
    expect(screen.queryByTestId("notification-badge")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("tsc.notifications.seenTs")).toBe("2026-10-20T12:00:03.000Z");
  });

  it("explains the empty inbox and keeps sound off by default", async () => {
    renderBell([]);
    await userEvent.click(await screen.findByRole("button", { name: /Notifications, 0 unread/ }));
    expect(screen.getByTestId("inbox-empty")).toBeInTheDocument();
    const sound = screen.getByRole("checkbox", { name: /Sound/ });
    expect(sound).not.toBeChecked();
    await userEvent.click(sound);
    expect(window.localStorage.getItem("tsc.notifications.sound")).toBe("on");
  });

  it("merges pushed deltas into the cache (rows by key, notifications newest first)", () => {
    const client = new QueryClient();
    const markets: MarketsResponse = {
      generated_at: "a",
      running: sourced(true, "SYSTEM"),
      rows: [marketRow("BTCUSDT", "LIVE"), marketRow("ETHUSDT", "LIVE")],
    };
    client.setQueryData(queryKeys.markets, markets);
    client.setQueryData(queryKeys.notifications, feed([note("e1")]));
    const stale = marketRow("ETHUSDT", "STALE");
    applyDelta(client, {
      as_of: "b",
      markets: [stale],
      notifications: [note("e2"), note("e3")],
      health_changed: false,
    });
    const merged = client.getQueryData<MarketsResponse>(queryKeys.markets)!;
    expect(merged.rows.map((r) => r.symbol)).toEqual(["BTCUSDT", "ETHUSDT"]);
    expect(merged.rows[1]!.best_bid_ask.provenance).toBe("STALE");
    expect(merged.rows[0]!.best_bid_ask.provenance).toBe("LIVE");
    const notes = client.getQueryData<NotificationsResponse>(queryKeys.notifications)!;
    expect(notes.notifications.value!.map((n) => n.id)).toEqual(["e3", "e2", "e1"]);
  });
});
