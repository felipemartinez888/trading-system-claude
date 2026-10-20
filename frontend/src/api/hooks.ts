// TanStack Query hooks plus the /ws/live subscription that keeps the status cache current.

import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, liveUrl, readToken } from "./client";
import type {
  DeltaMessage,
  LiveMessage,
  Market,
  MarketRow,
  MarketsResponse,
  NotificationsResponse,
  ShutdownInfo,
  StatusResponse,
} from "./types";

export const queryKeys = {
  status: ["status"] as const,
  coverage: ["coverage"] as const,
  events: ["events"] as const,
  markets: ["markets"] as const,
  streamsHealth: ["streams-health"] as const,
  gaps: ["gaps"] as const,
  bars: ["bars"] as const,
  signals: ["signals"] as const,
  notifications: ["notifications"] as const,
};

/** Chart window: the last six hours of stored 1m bars. */
export const BARS_WINDOW_MS = 6 * 60 * 60 * 1000;

/** Polling is a fallback; the WebSocket pushes every change and heartbeat. */
export const STATUS_POLL_MS = 10_000;

export function useStatus() {
  return useQuery({
    queryKey: queryKeys.status,
    queryFn: ({ signal }) => api.status(signal),
    refetchInterval: STATUS_POLL_MS,
  });
}

export function useCoverage() {
  return useQuery({
    queryKey: queryKeys.coverage,
    queryFn: ({ signal }) => api.coverage(signal),
    refetchInterval: 15_000,
  });
}

export function useEvents(limit = 200) {
  return useQuery({
    queryKey: [...queryKeys.events, limit],
    queryFn: ({ signal }) => api.events(limit, signal),
    refetchInterval: 15_000,
  });
}

export function useMarkets() {
  return useQuery({
    queryKey: queryKeys.markets,
    queryFn: ({ signal }) => api.markets(signal),
    refetchInterval: 5_000, // fallback; deltas arrive on /ws/live at most once per second
  });
}

export function useStreamsHealth() {
  return useQuery({
    queryKey: queryKeys.streamsHealth,
    queryFn: ({ signal }) => api.streamsHealth(signal),
    refetchInterval: 5_000,
  });
}

export function useGaps() {
  return useQuery({
    queryKey: queryKeys.gaps,
    queryFn: ({ signal }) => api.gaps(signal),
    refetchInterval: 15_000,
  });
}

export function useBars(market: Market, symbol: string) {
  return useQuery({
    queryKey: [...queryKeys.bars, market, symbol],
    queryFn: ({ signal }) => {
      const to = Date.now();
      return api.bars(
        market,
        symbol,
        new Date(to - BARS_WINDOW_MS).toISOString(),
        new Date(to + 60_000).toISOString(),
        signal,
      );
    },
    refetchInterval: 30_000,
  });
}

export function useSignals() {
  return useQuery({
    queryKey: queryKeys.signals,
    queryFn: ({ signal }) => api.signals(signal),
    refetchInterval: 15_000,
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: queryKeys.notifications,
    queryFn: ({ signal }) => api.notifications(signal),
    refetchInterval: 30_000,
  });
}

/** Merge one `delta` push message into the query cache (exported for tests). */
export function applyDelta(client: QueryClient, delta: DeltaMessage): void {
  if (delta.markets.length > 0) {
    const previous = client.getQueryData<MarketsResponse>(queryKeys.markets);
    if (previous === undefined) {
      void client.invalidateQueries({ queryKey: queryKeys.markets });
    } else {
      const key = (r: MarketRow) => `${r.market}:${r.symbol}`;
      const incoming = new Map(delta.markets.map((r) => [key(r), r]));
      let newBar = false;
      const rows = previous.rows.map((r) => {
        const next = incoming.get(key(r));
        if (next === undefined) return r;
        incoming.delete(key(r));
        if (next.last_closed_bar.value?.open_time_ms !== r.last_closed_bar.value?.open_time_ms) {
          newBar = true;
        }
        return next;
      });
      client.setQueryData<MarketsResponse>(queryKeys.markets, {
        ...previous,
        generated_at: delta.as_of,
        rows: [...rows, ...incoming.values()],
      });
      if (newBar) void client.invalidateQueries({ queryKey: queryKeys.bars });
    }
  }
  if (delta.notifications.length > 0) {
    const previous = client.getQueryData<NotificationsResponse>(queryKeys.notifications);
    if (previous?.notifications.value != null) {
      const known = new Set(previous.notifications.value.map((n) => n.id));
      const fresh = delta.notifications.filter((n) => !known.has(n.id));
      client.setQueryData<NotificationsResponse>(queryKeys.notifications, {
        ...previous,
        notifications: {
          ...previous.notifications,
          value: [...fresh.reverse(), ...previous.notifications.value],
        },
      });
    } else {
      void client.invalidateQueries({ queryKey: queryKeys.notifications });
    }
    if (delta.notifications.some((n) => n.kind === "signal_created")) {
      void client.invalidateQueries({ queryKey: queryKeys.signals });
    }
    if (delta.notifications.some((n) => n.kind.startsWith("gap_"))) {
      void client.invalidateQueries({ queryKey: queryKeys.gaps });
    }
  }
  if (delta.health_changed) {
    void client.invalidateQueries({ queryKey: queryKeys.streamsHealth });
  }
}

export type MonitoringAction = "start" | "stop";

export function useMonitoringMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: MonitoringAction) =>
      action === "start" ? api.startMonitoring() : api.stopMonitoring(),
    onSuccess: (status: StatusResponse) => {
      client.setQueryData(queryKeys.status, status);
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: queryKeys.status });
      void client.invalidateQueries({ queryKey: queryKeys.coverage });
      void client.invalidateQueries({ queryKey: queryKeys.events });
      void client.invalidateQueries({ queryKey: queryKeys.markets });
      void client.invalidateQueries({ queryKey: queryKeys.streamsHealth });
    },
  });
}

export function useQuitMutation() {
  return useMutation({ mutationFn: () => api.quit() });
}

/**
 * - `stopping`: the backend announced it is shutting down; the outcome is not known yet;
 * - `shutdown`: the backend reported the outcome (see `LiveLink.shutdown`, clean or not);
 * - `stopped-unconfirmed`: the connection ended during shutdown without an outcome message.
 */
export type LinkState =
  | "connecting"
  | "open"
  | "closed"
  | "no-token"
  | "stopping"
  | "shutdown"
  | "stopped-unconfirmed";

export interface LiveLink {
  state: LinkState;
  /** Outcome of closing the session, exactly as reported by the backend. */
  shutdown: ShutdownInfo | null;
}

/**
 * Subscribes to /ws/live and writes pushed status snapshots into the query cache.
 * Reconnects with capped backoff; the returned state is shown in the status bar so a dead
 * connection is never mistaken for a quiet backend.
 */
export function useLiveStatus(): LiveLink {
  const client = useQueryClient();
  const [state, setState] = useState<LinkState>(() =>
    readToken() === null ? "no-token" : "connecting",
  );
  const [shutdown, setShutdown] = useState<ShutdownInfo | null>(null);

  useEffect(() => {
    const token = readToken();
    if (token === null || typeof WebSocket === "undefined") {
      setState("no-token");
      return;
    }
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;
    let phase: "running" | "stopping" | "done" = "running";

    const connect = () => {
      if (stopped) return;
      setState("connecting");
      socket = new WebSocket(liveUrl(token));
      socket.onopen = () => {
        attempt = 0;
        setState("open");
      };
      socket.onmessage = (event: MessageEvent<string>) => {
        let message: LiveMessage;
        try {
          message = JSON.parse(event.data) as LiveMessage;
        } catch {
          return;
        }
        if (message.type === "status") {
          client.setQueryData(queryKeys.status, message.data);
        } else if (message.type === "delta") {
          applyDelta(client, message.data);
        } else if (message.type === "shutting_down") {
          if (phase === "running") phase = "stopping";
          setState("stopping");
        } else if (message.type === "shutdown") {
          phase = "done";
          setShutdown(message.data);
          setState("shutdown");
        }
      };
      socket.onclose = () => {
        socket = null;
        if (stopped) return;
        if (phase === "done") {
          setState("shutdown");
          return;
        }
        if (phase === "stopping") {
          setState("stopped-unconfirmed");
          return;
        }
        setState("closed");
        // The backend may be gone: make the status query re-check (it will surface the error).
        void client.invalidateQueries({ queryKey: queryKeys.status });
        const delay = Math.min(10_000, 500 * 2 ** attempt);
        attempt += 1;
        retryTimer = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      stopped = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [client]);

  return { state, shutdown };
}
