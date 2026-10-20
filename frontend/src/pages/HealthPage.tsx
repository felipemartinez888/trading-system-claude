// System › Health (M0): session, event-chain integrity and observation coverage (observed vs not).

import { useCoverage, useStatus } from "../api/hooks";
import type { CoverageGap, CoverageInterval } from "../api/types";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { durationBetween, formatUtc, shortHash } from "../format";
import { shown, useBackendFreshness } from "../freshness";
import { StreamsHealth } from "./StreamsHealth";

type Row =
  | { kind: "observed"; start: string; end: string | null; note: string }
  | { kind: "unobserved"; start: string; end: string | null; note: string };

function timeline(intervals: CoverageInterval[], gaps: CoverageGap[]): Row[] {
  const rows: Row[] = [
    ...intervals.map(
      (iv): Row => ({
        kind: "observed",
        start: iv.start,
        end: iv.end,
        note: iv.end === null ? "monitoring (still observing)" : `ended: ${iv.end_reason ?? "?"}`,
      }),
    ),
    ...gaps.map(
      (g): Row => ({
        kind: "unobserved",
        start: g.start,
        end: g.end,
        note: g.end === null ? "not observing now" : `after: ${g.after_reason ?? "?"}`,
      }),
    ),
  ];
  // newest first
  return rows.sort((a, b) => Date.parse(b.start) - Date.parse(a.start));
}

export function HealthPage() {
  const status = useStatus();
  const coverage = useCoverage();
  const freshness = useBackendFreshness();
  const s = status.data;
  const chain = s?.chain.value ?? null;
  const session = s?.session.value ?? null;
  const prev = s?.previous_session.value ?? null;

  return (
    <div className="page">
      <h1 className="page__title">System › Health</h1>

      <section className="card" aria-labelledby="h-session">
        <h2 id="h-session" className="card__title">
          App session {s ? <ProvenanceBadge {...shown(s.session, freshness)} compact /> : null}
        </h2>
        {session === null ? (
          <p className="muted">{s?.session.reason ?? "Waiting for the backend…"}</p>
        ) : (
          <dl className="kv">
            <dt>Session id</dt>
            <dd className="mono">{session.session_id}</dd>
            <dt>Started</dt>
            <dd>{formatUtc(session.started_at)}</dd>
            <dt>Last heartbeat</dt>
            <dd>
              {formatUtc(s?.heartbeat.value)}{" "}
              {s ? <ProvenanceBadge {...shown(s.heartbeat, freshness)} compact /> : null}
            </dd>
            <dt>Previous session</dt>
            <dd>
              {prev === null
                ? "—"
                : prev.outcome === "NONE"
                  ? "none (first run on this data directory)"
                  : prev.outcome === "CLEAN"
                    ? `ended cleanly at ${formatUtc(prev.ended_at)} (${prev.end_reason ?? "?"})`
                    : prev.outcome === "CRASHED"
                      ? `did NOT shut down cleanly; last alive ${formatUtc(prev.ended_at)}`
                      : `unknown (${prev.end_reason ?? "no record"})`}
            </dd>
          </dl>
        )}
      </section>

      <section className="card" aria-labelledby="h-chain">
        <h2 id="h-chain" className="card__title">
          Event-chain integrity {s ? <ProvenanceBadge {...shown(s.chain, freshness)} compact /> : null}
        </h2>
        {chain === null ? (
          <p className="muted">{s?.chain.reason ?? "Waiting for the backend…"}</p>
        ) : (
          <dl className="kv">
            <dt>Result</dt>
            <dd className={chain.ok ? "ok" : "bad"}>
              {chain.ok
                ? "verified"
                : `FAILED at seq ${chain.first_bad_seq ?? "?"}: ${chain.reason ?? "?"} — monitoring is locked`}
            </dd>
            <dt>Events checked</dt>
            <dd>{chain.events_checked}</dd>
            <dt>Head</dt>
            <dd className="mono" title={chain.head_hash}>
              seq {chain.head_seq} · {shortHash(chain.head_hash)}
            </dd>
            <dt>Verified at</dt>
            <dd>{formatUtc(chain.verified_at)} (at app start)</dd>
          </dl>
        )}
      </section>

      <StreamsHealth />

      <section className="card" aria-labelledby="h-coverage">
        <h2 id="h-coverage" className="card__title">
          Observation coverage{" "}
          {coverage.data ? (
            <ProvenanceBadge {...shown(coverage.data.intervals, freshness)} compact />
          ) : null}
        </h2>
        <p className="muted">
          Only time inside a monitoring interval counts as observed. Unobserved time can never
          produce live signals; market data for it can only be backfilled (startup backfill, tagged accordingly).
        </p>
        {coverage.isError ? (
          <p className="bad" role="alert">
            Coverage could not be loaded: {String(coverage.error)}
          </p>
        ) : coverage.data === undefined ? (
          <p className="muted">Loading…</p>
        ) : coverage.data.intervals.value === null || coverage.data.gaps.value === null ? (
          <p className="bad">{coverage.data.intervals.reason ?? coverage.data.gaps.reason}</p>
        ) : coverage.data.intervals.value.length === 0 ? (
          <p data-testid="coverage-empty">
            No monitoring intervals recorded yet. Press <strong>Start</strong> in the status bar
            to begin observing; the interval appears here when monitoring starts.
          </p>
        ) : (
          <table className="table" data-testid="coverage-table">
            <thead>
              <tr>
                <th scope="col">Coverage</th>
                <th scope="col">From (UTC)</th>
                <th scope="col">To (UTC)</th>
                <th scope="col">Duration</th>
                <th scope="col">Note</th>
              </tr>
            </thead>
            <tbody>
              {timeline(coverage.data.intervals.value, coverage.data.gaps.value).map((row) => (
                <tr key={`${row.kind}-${row.start}`} className={`row--${row.kind}`}>
                  <td>
                    <span className={`chip ${row.kind === "observed" ? "chip--ok" : "chip--gap"}`}>
                      {row.kind === "observed" ? "observed" : "not observed"}
                    </span>
                  </td>
                  <td>{formatUtc(row.start)}</td>
                  <td>{row.end === null ? "now" : formatUtc(row.end)}</td>
                  <td>{durationBetween(row.start, row.end)}</td>
                  <td>{row.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
