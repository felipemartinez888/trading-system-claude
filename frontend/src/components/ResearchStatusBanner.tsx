// Research status (MVP), from GET /api/research/status. It never shows a green "validated"
// indicator on its own: the backend decides the state from verified, preregistered runs only, and
// a missing or unreadable status is shown as unknown, never as "fine".

import { useResearchStatus } from "../api/hooks";
import type { ResearchStatus, Sourced } from "../api/types";

export interface ResearchStatusViewProps {
  status: Sourced<ResearchStatus> | undefined;
  error?: unknown;
}

const TIER_TEXT: Record<string, string> = {
  REFERENCE: "REFERENCE / NOT VALIDATED",
  VALIDATED: "VALIDATED (grade A)",
  REJECTED: "REJECTED (grade D)",
  CANDIDATE: "CANDIDATE / NOT VALIDATED",
  UNREGISTERED: "UNREGISTERED / NOT EVIDENCE",
};

export function ResearchStatusView({ status, error }: ResearchStatusViewProps) {
  if (error !== undefined && error !== null) {
    return (
      <section className="panel" data-testid="research-status" role="status">
        <p className="muted">Research status unknown: {String(error)}</p>
      </section>
    );
  }
  if (status === undefined) {
    return (
      <section className="panel" data-testid="research-status" role="status">
        <p className="muted">Research status loading…</p>
      </section>
    );
  }
  const value = status.value;
  if (value === null) {
    return (
      <section className="panel" data-testid="research-status" role="status">
        <p className="muted">Research status unknown: {status.reason ?? "unavailable"}</p>
      </section>
    );
  }
  const none = value.state !== "VALIDATED_STRATEGY_AVAILABLE";
  return (
    <section className="panel" data-testid="research-status" role="status">
      <p className={none ? "bad" : undefined}>
        <strong>{none ? "NO VALIDATED STRATEGY" : `${value.validated} strategy(ies) graded A`}</strong>
        {" — "}
        {value.statement}
      </p>
      {value.reasons.length > 0 ? (
        <ul className="muted" data-testid="research-status-reasons">
          {value.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      <table className="table" data-testid="research-status-strategies">
        <thead>
          <tr>
            <th>Strategy</th>
            <th>Tier</th>
            <th>Verified preregistered runs</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {value.strategies.map((s) => (
            <tr key={s.strategy_id} data-testid={`research-tier-${s.strategy_id}`}>
              <td>
                <code>{s.strategy_id}</code>
              </td>
              <td>{TIER_TEXT[s.tier] ?? s.tier}</td>
              <td>
                {s.trusted_runs} of {s.runs}
              </td>
              <td className="muted">{s.reasons.join("; ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function ResearchStatusBanner() {
  const query = useResearchStatus();
  return (
    <ResearchStatusView
      status={query.data?.status}
      error={query.isError ? query.error : undefined}
    />
  );
}
