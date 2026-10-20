// Persistent status bar: always the real backend state (AC-02). Nothing here is ever shown as
// healthy unless the API said so; stream groups stay UNAVAILABLE until M1 delivers them.

import { useEffect, useState } from "react";

import { ApiError } from "../api/client";
import {
  type LinkState,
  type MonitoringAction,
  useMonitoringMutation,
  useQuitMutation,
  useStatus,
} from "../api/hooks";
import type { Provenance, Sourced, StatusResponse } from "../api/types";
import { formatDuration, formatUtc } from "../format";
import { ProvenanceBadge } from "./ProvenanceBadge";

/** Client-side freshness budget for the last status snapshot, in heartbeats. */
const CLIENT_STALE_HEARTBEATS = 3;

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 423) return `Refused (locked): ${error.detail}`;
    if (error.status === 409) return `Not allowed now: ${error.detail}`;
    if (error.status === 403) return `Forbidden: ${error.detail}`;
    return error.message;
  }
  if (error instanceof Error) return `Backend unreachable: ${error.message}`;
  return "Unknown error";
}

/**
 * The UI may only *downgrade* provenance (never upgrade it): when the last snapshot is older than
 * the freshness budget, LIVE values are shown as STALE with the reason.
 */
function displayed<T>(
  sourced: Sourced<T>,
  stale: boolean,
): { provenance: Provenance; reason: string | null } {
  if (stale && sourced.provenance === "LIVE") {
    return { provenance: "STALE", reason: "no fresh update from the backend" };
  }
  return { provenance: sourced.provenance, reason: sourced.reason };
}

export interface StatusBarProps {
  link: LinkState;
}

export function StatusBar({ link }: StatusBarProps) {
  const status = useStatus();
  const monitoring = useMonitoringMutation();
  const quit = useQuitMutation();
  const now = useNow();

  const data: StatusResponse | undefined = status.data;
  const budgetMs = (data?.heartbeat_interval_s ?? 5) * 1000 * CLIENT_STALE_HEARTBEATS;
  const stale = data !== undefined && now - status.dataUpdatedAt > budgetMs;
  const quitDone = quit.isSuccess || link === "shutdown";

  if (quitDone) {
    return (
      <header className="statusbar statusbar--ended" role="banner">
        <strong>Trading System Claude has stopped.</strong>
        <span>The session was closed cleanly. You can close this tab.</span>
      </header>
    );
  }

  if (data === undefined) {
    return (
      <header className="statusbar" role="banner">
        <span className="statusbar__brand">TSC</span>
        {status.isError ? (
          <span className="statusbar__error" role="alert">
            {errorText(status.error)}
          </span>
        ) : (
          <span aria-live="polite">Connecting to the local backend…</span>
        )}
      </header>
    );
  }

  const mon = data.monitoring.value;
  const session = data.session.value;
  const chain = data.chain.value;
  const prev = data.previous_session.value;
  const state = mon?.state ?? null;
  const pendingAction: MonitoringAction | null = monitoring.isPending
    ? (monitoring.variables ?? null)
    : null;
  const action: MonitoringAction = state === "RUNNING" ? "stop" : "start";
  const canAct =
    !monitoring.isPending &&
    !stale &&
    mon !== null &&
    (action === "stop" || mon.start_allowed);
  const uptime =
    session === null
      ? null
      : session.uptime_s + Math.max(0, (now - status.dataUpdatedAt) / 1000);
  const monDisplay = displayed(data.monitoring, stale);

  return (
    <header className="statusbar" role="banner">
      <span className="statusbar__brand">TSC</span>

      <section className="statusbar__group" aria-label="Monitoring">
        <span
          className={`statusbar__state statusbar__state--${(state ?? "unknown").toLowerCase()}`}
          data-testid="monitoring-state"
        >
          <span className="dot" aria-hidden="true" />
          {state === null ? "UNKNOWN" : state === "RUNNING" ? "MONITORING" : "STOPPED"}
        </span>
        <ProvenanceBadge
          provenance={monDisplay.provenance}
          reason={monDisplay.reason ?? mon?.blocked_reason ?? null}
          asOfMs={data.monitoring.as_of_ms}
          compact
        />
        <button
          type="button"
          className={`btn ${action === "stop" ? "btn--stop" : "btn--start"}`}
          disabled={!canAct}
          aria-busy={monitoring.isPending}
          title={mon?.blocked_reason ?? undefined}
          onClick={() => monitoring.mutate(action)}
        >
          {pendingAction === "start"
            ? "Starting…"
            : pendingAction === "stop"
              ? "Stopping…"
              : action === "stop"
                ? "Stop"
                : "Start"}
        </button>
      </section>

      <section className="statusbar__group" aria-label="Session">
        <span className="statusbar__label">Session</span>
        <span data-testid="session-uptime">{uptime === null ? "—" : formatDuration(uptime)}</span>
        <ProvenanceBadge {...displayed(data.session, stale)} asOfMs={data.session.as_of_ms} compact />
      </section>

      <section className="statusbar__group" aria-label="Event chain">
        <span className="statusbar__label">Chain</span>
        {chain === null ? (
          <ProvenanceBadge provenance="UNAVAILABLE" reason={data.chain.reason} compact />
        ) : chain.ok ? (
          <span className="chip chip--ok" title={`${chain.events_checked} events verified`}>
            verified · {chain.events_checked}
          </span>
        ) : (
          <span className="chip chip--bad" role="alert">
            FAILED at seq {chain.first_bad_seq ?? "?"}: {chain.reason}
          </span>
        )}
      </section>

      <section className="statusbar__group" aria-label="Market data streams">
        {Object.entries(data.streams).map(([name, stream]) => (
          <span key={name} className="statusbar__stream" data-testid={`stream-${name}`}>
            <span className="statusbar__label">{name === "spot" ? "Spot" : name === "futures" ? "Futures" : name}</span>
            {stream.value !== null ? (
              <span>{stream.value}</span>
            ) : null}
            <ProvenanceBadge provenance={stream.provenance} reason={stream.reason} compact />
            {stream.provenance === "UNAVAILABLE" && stream.reason ? (
              <span className="statusbar__muted">— {stream.reason}</span>
            ) : null}
          </span>
        ))}
      </section>

      <section className="statusbar__group statusbar__group--end" aria-label="Connection">
        <span className={`link link--${link}`} title="Live push channel (/ws/live)">
          push: {link}
        </span>
        <button
          type="button"
          className="btn btn--ghost"
          disabled={quit.isPending}
          onClick={() => {
            if (window.confirm("Quit Trading System Claude? Monitoring will stop.")) quit.mutate();
          }}
        >
          {quit.isPending ? "Quitting…" : "Quit"}
        </button>
      </section>

      {stale || status.isError ? (
        <div className="statusbar__notice statusbar__notice--warn" role="alert">
          Backend not responding — showing the last known state from{" "}
          {formatUtc(new Date(status.dataUpdatedAt).toISOString())}.
          {status.isError ? ` ${errorText(status.error)}` : ""}
        </div>
      ) : null}

      {monitoring.isError ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          {errorText(monitoring.error)}
          <button type="button" className="btn btn--ghost" onClick={() => monitoring.reset()}>
            Dismiss
          </button>
        </div>
      ) : null}

      {quit.isError ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          Quit failed: {errorText(quit.error)}
        </div>
      ) : null}

      {mon !== null && !mon.start_allowed && mon.blocked_reason ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          Monitoring is locked: {mon.blocked_reason}. See System › Health.
        </div>
      ) : null}

      {prev !== null && prev.outcome === "CRASHED" ? (
        <div className="statusbar__notice statusbar__notice--warn" role="status" data-testid="crash-notice">
          The previous session ({prev.session_id}) did not shut down cleanly
          {prev.monitoring_was_active ? " while monitoring was running" : ""}. Not observed from{" "}
          <time dateTime={prev.unobserved_from ?? undefined}>{formatUtc(prev.unobserved_from)}</time> to{" "}
          <time dateTime={prev.unobserved_to ?? undefined}>{formatUtc(prev.unobserved_to)}</time>.
        </div>
      ) : null}
    </header>
  );
}
