// Persistent status bar: always the real backend state (AC-02). Nothing here is ever shown as
// healthy unless the API said so; stream groups stay UNAVAILABLE until M1 delivers them.

import { ApiError } from "../api/client";
import {
  type LiveLink,
  type MonitoringAction,
  useMonitoringMutation,
  useQuitMutation,
  useStatus,
} from "../api/hooks";
import type { ShutdownInfo, StatusResponse } from "../api/types";
import { formatDuration, formatUtc } from "../format";
import { shown, useBackendFreshness, useNow } from "../freshness";
import { NotificationBell } from "./Notifications";
import { ProvenanceBadge } from "./ProvenanceBadge";

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

/** Shown once the app is stopping; says only what the backend actually reported. */
function EndedBar({ outcome, state }: { outcome: ShutdownInfo | null; state: LiveLink["state"] }) {
  let title: string;
  let body: string;
  let tone: "ok" | "warn" | "bad";
  if (outcome !== null && outcome.clean) {
    title = "Trading System Claude has stopped.";
    body = `The session was closed cleanly (session_stopped recorded, reason: ${outcome.reason}). You can close this tab.`;
    tone = "ok";
  } else if (outcome !== null) {
    title = "Trading System Claude stopped, but the session was NOT closed cleanly.";
    body = outcome.detail ?? "The backend reported a failure while closing the session.";
    tone = "bad";
  } else if (state === "stopping") {
    title = "Trading System Claude is shutting down…";
    body = "Waiting for the backend to confirm that the session was closed.";
    tone = "warn";
  } else {
    title = "Trading System Claude is no longer reachable.";
    body =
      "It was shutting down, but whether the session was closed cleanly could not be confirmed. " +
      "The next start shows how the previous session ended.";
    tone = "warn";
  }
  return (
    <header className={`statusbar statusbar--ended statusbar--ended-${tone}`} role="banner">
      <strong data-testid="ended-title">{title}</strong>
      <span data-testid="ended-body">{body}</span>
    </header>
  );
}

export interface StatusBarProps {
  live: LiveLink;
}

export function StatusBar({ live }: StatusBarProps) {
  const status = useStatus();
  const monitoring = useMonitoringMutation();
  const cancelStart = useMonitoringMutation(); // separate: the start request is still pending
  const quit = useQuitMutation();
  const now = useNow();
  const freshness = useBackendFreshness();
  const link = live.state;

  const data: StatusResponse | undefined = status.data;
  const stale = freshness.stale;
  const quitOutcome = quit.data?.accepted ? (quit.data.session.value ?? null) : null;
  const outcome = quitOutcome ?? live.shutdown;
  const ending =
    outcome !== null ||
    link === "stopping" ||
    link === "shutdown" ||
    link === "stopped-unconfirmed";

  if (ending) {
    return <EndedBar outcome={outcome} state={link} />;
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
  const serverTransition = mon?.transition ?? null;
  // N1: Stop stays available whenever the backend answers, even if values are STALE (stopping is
  // always the safe direction). Start additionally needs fresh state and the backend's consent.
  const backendAnswers = !status.isError;
  const canAct =
    !monitoring.isPending &&
    serverTransition === null &&
    mon !== null &&
    backendAnswers &&
    (action === "stop" || (mon.start_allowed && !stale));
  const starting = pendingAction === "start" || serverTransition === "starting";
  // A start the user cancelled is reported by the backend as 409 "start cancelled …": expected,
  // not an error to show.
  const startCancelled =
    monitoring.error instanceof ApiError && monitoring.error.detail.includes("start cancelled");
  const uptime =
    session === null
      ? null
      : session.uptime_s + Math.max(0, (now - status.dataUpdatedAt) / 1000);
  const monDisplay = shown(data.monitoring, freshness);
  const chainDisplay = shown(data.chain, freshness);
  const prevDisplay = shown(data.previous_session, freshness);

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
          asOfMs={monDisplay.asOfMs}
          compact
        />
        {starting ? (
          <>
            <span className="statusbar__muted" aria-live="polite" data-testid="starting">
              Starting…
            </span>
            {/* Review #10: a slow start (REST timeouts) can be cancelled; the backend cancels the
                in-flight start cleanly and monitoring stays STOPPED. */}
            <button
              type="button"
              className="btn btn--stop"
              disabled={cancelStart.isPending || !backendAnswers}
              onClick={() => cancelStart.mutate("stop")}
            >
              {cancelStart.isPending ? "Cancelling…" : "Cancel start"}
            </button>
          </>
        ) : (
          <button
            type="button"
            className={`btn ${action === "stop" ? "btn--stop" : "btn--start"}`}
            disabled={!canAct}
            aria-busy={monitoring.isPending}
            title={mon?.blocked_reason ?? undefined}
            onClick={() => monitoring.mutate(action)}
          >
            {pendingAction === "stop" || serverTransition === "stopping"
              ? "Stopping…"
              : action === "stop"
                ? "Stop"
                : "Start"}
          </button>
        )}
      </section>

      <section className="statusbar__group" aria-label="Session">
        <span className="statusbar__label">Session</span>
        <span data-testid="session-uptime">{uptime === null ? "—" : formatDuration(uptime)}</span>
        <ProvenanceBadge {...shown(data.session, freshness)} compact />
      </section>

      <section className="statusbar__group" aria-label="Event chain">
        <span className="statusbar__label">Chain</span>
        {chain === null ? null : chain.ok ? (
          <span
            className={`chip chip--ok${stale ? " chip--stale" : ""}`}
            title={`${chain.events_checked} events verified at startup`}
          >
            verified · {chain.events_checked}
          </span>
        ) : (
          <span className={`chip chip--bad${stale ? " chip--stale" : ""}`} role="alert">
            FAILED at seq {chain.first_bad_seq ?? "?"}: {chain.reason}
          </span>
        )}
        <span data-testid="chain-provenance">
          <ProvenanceBadge {...chainDisplay} compact />
        </span>
      </section>

      <section className="statusbar__group" aria-label="Market data streams">
        {Object.entries(data.streams).map(([name, stream]) => {
          const display = shown(stream, freshness);
          // Green only when the backend says every stream is HEALTHY and the value is current.
          const healthy = display.provenance === "SYSTEM" && (stream.value ?? "").startsWith("HEALTHY");
          return (
            <span key={name} className="statusbar__stream" data-testid={`stream-${name}`}>
              <span className="statusbar__label">
                {name === "spot" ? "Spot" : name === "futures" ? "Futures" : name}
              </span>
              {stream.value !== null ? (
                <span className={healthy ? "stream-ok" : "stream-bad"}>{stream.value}</span>
              ) : null}
              <ProvenanceBadge {...display} compact />
              {stream.provenance === "UNAVAILABLE" && stream.reason ? (
                <span className="statusbar__muted">— {stream.reason}</span>
              ) : null}
            </span>
          );
        })}
      </section>

      <section className="statusbar__group statusbar__group--end" aria-label="Connection">
        <span className={`link link--${link}`} title="Live push channel (/ws/live)">
          push: {link}
        </span>
        <NotificationBell />
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

      {stale ? (
        <div className="statusbar__notice statusbar__notice--warn" role="alert">
          <span>
            Values are STALE ({freshness.reason}) — showing the last known state from{" "}
            {formatUtc(new Date(status.dataUpdatedAt).toISOString())}.
            {status.isError ? ` ${errorText(status.error)}` : ""}
          </span>
        </div>
      ) : null}

      {cancelStart.isError ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          <span>Cancelling the start failed: {errorText(cancelStart.error)}</span>
        </div>
      ) : null}

      {monitoring.isError && !startCancelled ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          <span>{errorText(monitoring.error)}</span>
          <button type="button" className="btn btn--ghost" onClick={() => monitoring.reset()}>
            Dismiss
          </button>
        </div>
      ) : null}

      {quit.data !== undefined && !quit.data.accepted ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          <span>Quit was not accepted: {quit.data.session.reason ?? "unknown reason"}</span>
        </div>
      ) : null}

      {quit.isError ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          <span>Quit failed: {errorText(quit.error)}</span>
        </div>
      ) : null}

      {mon !== null && mon.last_error !== null && state === "STOPPED" ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert" data-testid="start-error">
          <span>Monitoring could not be started: {mon.last_error}. It stays STOPPED.</span>
        </div>
      ) : null}

      {mon !== null && !mon.start_allowed && mon.blocked_reason ? (
        <div className="statusbar__notice statusbar__notice--error" role="alert">
          <span>Monitoring is locked: {mon.blocked_reason}. See System › Health.</span>
        </div>
      ) : null}

      {prev !== null && prev.outcome === "CRASHED" ? (
        <div className="statusbar__notice statusbar__notice--warn" role="status" data-testid="crash-notice">
          <span data-testid="crash-provenance">
            <ProvenanceBadge {...prevDisplay} compact />
          </span>
          <span>
            The previous session ({prev.session_id}) did not shut down cleanly
            {prev.monitoring_was_active ? " while monitoring was running" : ""}. Not observed from{" "}
            <time dateTime={prev.unobserved_from ?? undefined}>{formatUtc(prev.unobserved_from)}</time>{" "}
            to <time dateTime={prev.unobserved_to ?? undefined}>{formatUtc(prev.unobserved_to)}</time>.
          </span>
        </div>
      ) : null}
    </header>
  );
}
