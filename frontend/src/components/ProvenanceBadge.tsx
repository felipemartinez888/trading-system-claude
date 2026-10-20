// One badge per displayed value, driven only by the API-provided provenance (ADR-0010).

import type { ReactNode } from "react";

import type { Provenance } from "../api/types";

interface Meta {
  label: string;
  description: string;
}

export const PROVENANCE_META: Record<Provenance, Meta> = {
  LIVE: { label: "LIVE", description: "Market data observed live from the exchange; fresh" },
  SYSTEM: {
    label: "SYSTEM",
    description: "The app's own runtime state, read from the backend (not market data)",
  },
  PARTIAL: { label: "PARTIAL", description: "Forming candle or incomplete aggregate" },
  RECOVERED: {
    label: "RECOVERED",
    description: "Filled after the fact from REST for a period that was being observed",
  },
  HISTORICAL: { label: "HISTORICAL", description: "Archive or backfill; never observed live" },
  REPLAY: { label: "REPLAY", description: "Produced by replaying recorded data" },
  BACKTEST: { label: "BACKTEST", description: "Produced by a historical simulation" },
  SIMULATED: { label: "SIMULATED", description: "Paper-trading result on live observations" },
  ESTIMATED: { label: "ESTIMATED", description: "Model-based estimate, not an observation" },
  DEMO: { label: "DEMO", description: "Fabricated demonstration data — not real" },
  STALE: { label: "STALE", description: "Last known value; freshness budget exceeded" },
  UNAVAILABLE: { label: "UNAVAILABLE", description: "No value available" },
};

export interface ProvenanceBadgeProps {
  provenance: Provenance;
  /** Shown as part of the tooltip/accessible name (e.g. the UNAVAILABLE reason). */
  reason?: string | null;
  /** Epoch ms the value refers to; shown in the tooltip. */
  asOfMs?: number | null;
  compact?: boolean;
}

export function ProvenanceBadge({ provenance, reason, asOfMs, compact = false }: ProvenanceBadgeProps) {
  const meta = PROVENANCE_META[provenance];
  const details = [meta.description];
  if (reason) details.push(reason);
  if (asOfMs !== null && asOfMs !== undefined) {
    details.push(`as of ${new Date(asOfMs).toISOString()}`);
  }
  const title = details.join(" — ");
  const className = [
    "prov",
    `prov--${provenance.toLowerCase()}`,
    provenance === "DEMO" ? "prov--hatched" : "",
    compact ? "prov--compact" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span
      className={className}
      data-provenance={provenance}
      title={title}
      aria-label={`provenance ${meta.label}: ${title}`}
      role="img"
    >
      {meta.label}
    </span>
  );
}

/** Wraps demo content with the diagonal-hatch watermark and a banner (DASHBOARD §2). */
export function DemoWatermark({
  children,
  banner = "DEMO — fabricated demonstration data, not market observations",
}: {
  children: ReactNode;
  banner?: string;
}) {
  return (
    <div className="demo-watermark" data-testid="demo-watermark">
      <div className="demo-watermark__banner" role="note">
        {banner}
      </div>
      {children}
    </div>
  );
}
