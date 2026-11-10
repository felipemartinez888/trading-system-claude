// Advisory agents (M5), from GET /api/agents/status: mode, budget and whether the gate is wired.
// Agents only advise (ADR-0007); a missing or unreadable status is shown as unknown, never as fine.

import type { ReactNode } from "react";

import { useAgentsStatus } from "../api/hooks";
import type { AgentStatus, Sourced } from "../api/types";

export interface AgentStatusViewProps {
  status: Sourced<AgentStatus> | undefined;
  error?: unknown;
}

export function AgentStatusView({ status, error }: AgentStatusViewProps) {
  const body = (content: ReactNode) => (
    <section className="card" aria-labelledby="h-agents" data-testid="agent-status">
      <h2 className="card__title" id="h-agents">
        Advisory agents
      </h2>
      {content}
    </section>
  );
  if (error !== undefined && error !== null) {
    return body(<p className="muted">Agent status unknown: {String(error)}</p>);
  }
  if (status === undefined) {
    return body(<p className="muted">Agent status loading…</p>);
  }
  const v = status.value;
  if (v === null) {
    return body(<p className="muted">Agent status unknown: {status.reason ?? "unavailable"}</p>);
  }
  const off = v.mode === "off";
  return body(
    <>
      <p data-testid="agent-mode">
        <strong>Mode: {off ? "OFF" : v.mode}</strong>
        {off ? " — no signal is sent to any model." : null}
      </p>
      {!v.config_ok ? (
        <p className="bad" data-testid="agent-config-errors">
          Configuration invalid (agents forced off): {v.config_errors.join("; ")}
        </p>
      ) : null}
      {!v.runtime_wired ? (
        <p className="muted" data-testid="agent-not-wired">
          {v.note}
        </p>
      ) : null}
      {v.ledger.error !== null ? (
        <p className="bad" data-testid="agent-ledger-error">
          Cost ledger: {v.ledger.error} (every review is refused)
        </p>
      ) : (
        <p data-testid="agent-budget">
          Spent today ${v.ledger.spent_today_usd ?? "0"} of ${v.daily_cap_usd} cap ·{" "}
          {v.ledger.calls_today ?? 0} call(s) today
        </p>
      )}
    </>,
  );
}

export function AgentStatusCard() {
  const query = useAgentsStatus();
  return (
    <AgentStatusView
      status={query.data?.status}
      error={query.isError ? query.error : undefined}
    />
  );
}
