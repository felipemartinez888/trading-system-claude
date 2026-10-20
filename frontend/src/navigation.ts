// Information architecture (DASHBOARD.md §1): five workspaces, each with its pages.
// Every page that has no data yet states what will appear and in which milestone (ROADMAP.md).

import { useCallback, useSyncExternalStore } from "react";

export interface PageDef {
  id: string;
  title: string;
  path: string;
  /** Empty state: what will appear here. */
  willShow: string;
  /** Empty state: when (milestone) and what produces it. */
  arrives: string;
  /** Pages with real M0 content render a dedicated component instead of the empty state. */
  implemented?: boolean;
}

export interface WorkspaceDef {
  id: string;
  title: string;
  pages: PageDef[];
}

export const WORKSPACES: WorkspaceDef[] = [
  {
    id: "live",
    title: "Live",
    pages: [
      {
        id: "markets",
        title: "Markets",
        path: "/live/markets",
        willShow:
          "Watchlist (spot and perpetual price, 24h change, ATR percentile, regime, spread, " +
          "funding, freshness) and a candlestick chart with signals and gap shading.",
        arrives:
          "M1 — Live tape: Binance spot and USDⓈ-M public streams for the configured symbols, " +
          "shown only while monitoring is running.",
      },
      {
        id: "scanner",
        title: "Scanner",
        path: "/live/scanner",
        willShow:
          "Early Watch candidates ranked by a strategy-defined score, with the conditions met or " +
          "pending and time to expiry.",
        arrives: "M4 — pre-registered strategies promoted to the live scanner.",
      },
      {
        id: "signals",
        title: "Signals",
        path: "/live/signals",
        willShow:
          "Signal inbox (new, watching, confirmed, open, closed, vetoed, expired) with a detail " +
          "drawer: summary, evidence, agent review, timeline and outcome.",
        arrives:
          "M1 — a clearly labeled demo detector emits WATCH events; real strategy signals in M4; " +
          "agent review in M5.",
      },
    ],
  },
  {
    id: "paper",
    title: "Paper",
    pages: [
      {
        id: "portfolio",
        title: "Portfolio",
        path: "/paper/portfolio",
        willShow: "Equity per paper book (baseline, agent-gated, benchmark) and the kill switch.",
        arrives: "M3 — Paper trading and risk engine.",
      },
      {
        id: "positions",
        title: "Positions & Orders",
        path: "/paper/positions",
        willShow: "Open simulated positions with unrealized P&L (STALE when data is), orders and fills.",
        arrives: "M3 — Paper trading and risk engine.",
      },
      {
        id: "ledger",
        title: "Ledger",
        path: "/paper/ledger",
        willShow: "Ledger entries per book with reconciliation status.",
        arrives: "M3 — Paper trading and risk engine.",
      },
      {
        id: "books",
        title: "Books compare",
        path: "/paper/books",
        willShow: "Baseline vs agent-gated vs benchmark books side by side.",
        arrives: "M3 (baseline and benchmark); agent-gated book in M5.",
      },
    ],
  },
  {
    id: "research",
    title: "Research",
    pages: [
      {
        id: "strategies",
        title: "Strategies & pre-registrations",
        path: "/research/strategies",
        willShow: "Frozen strategy versions and their committed pre-registration files.",
        arrives: "M4 — Research protocol and first strategies.",
      },
      {
        id: "backtests",
        title: "Backtests",
        path: "/research/backtests",
        willShow:
          "Backtest runs with grade chips; run detail with equity, drawdown, trades, costs, " +
          "bootstrap confidence intervals and the reproducibility manifest.",
        arrives: "M2 — Historical data and backtest core.",
      },
      {
        id: "validation",
        title: "Validation",
        path: "/research/validation",
        willShow:
          "Walk-forward folds, parameter neighbourhoods, trial counter and holdout status.",
        arrives: "M4 — Research protocol and first strategies.",
      },
    ],
  },
  {
    id: "performance",
    title: "Performance",
    pages: [
      {
        id: "analytics",
        title: "Analytics",
        path: "/performance/analytics",
        willShow: "Results by strategy, asset, regime and hour.",
        arrives: "M6 — Performance analytics and evaluation export.",
      },
      {
        id: "agent-value",
        title: "Agent value",
        path: "/performance/agent-value",
        willShow: "Whether agent review improved outcomes versus the baseline it filters.",
        arrives: "M6 (needs agent reviews from M5).",
      },
      {
        id: "export",
        title: "Evaluation export",
        path: "/performance/export",
        willShow: "JSONL evaluation export validated against its schema.",
        arrives: "M6 — Performance analytics and evaluation export.",
      },
    ],
  },
  {
    id: "system",
    title: "System",
    pages: [
      {
        id: "health",
        title: "Health",
        path: "/system/health",
        willShow:
          "Session, event-chain integrity and observation coverage now; stream table, " +
          "freshness, gaps and clock offset from M1.",
        arrives: "M0 (session, chain, coverage); streams and data quality in M1.",
        implemented: true,
      },
      {
        id: "logs",
        title: "Logs & audit",
        path: "/system/logs",
        willShow: "Recent events from the hash-chained event log.",
        arrives: "M0.",
        implemented: true,
      },
      {
        id: "costs",
        title: "Costs",
        path: "/system/costs",
        willShow: "Claude API spend per signal and against the budget.",
        arrives: "M5 — Claude signal agents.",
      },
      {
        id: "settings",
        title: "Settings",
        path: "/system/settings",
        willShow:
          "Universe, timeframes, risk limits (versioned), agent mode and budget, cost model, " +
          "retention and runtime options.",
        arrives: "Not scheduled in the roadmap yet; settings persistence is out of scope for M0.",
      },
    ],
  },
];

export const ALL_PAGES: PageDef[] = WORKSPACES.flatMap((w) => w.pages);

/** In M0 the useful content lives in System › Health, so the shell opens there. */
export const DEFAULT_PATH = "/system/health";

export function findPage(path: string): PageDef | undefined {
  const normalized = path.replace(/\/+$/, "") || "/";
  return ALL_PAGES.find((p) => p.path === normalized);
}

// ---------------------------------------------------------------------- minimal router
// History-API routing without a dependency; the backend serves index.html for client routes.

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("popstate", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("popstate", listener);
  };
}

export function navigate(path: string): void {
  if (window.location.pathname === path) return;
  window.history.pushState(null, "", path);
  listeners.forEach((l) => l());
}

export function usePath(): [string, (path: string) => void] {
  const path = useSyncExternalStore(
    subscribe,
    () => window.location.pathname,
    () => "/",
  );
  const go = useCallback((p: string) => navigate(p), []);
  return [path, go];
}
