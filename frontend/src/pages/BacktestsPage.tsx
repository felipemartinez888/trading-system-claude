// Research › Backtests (MVP-4): recorded runs from /api/backtests, read-only. Every value is
// BACKTEST and comes from the run's own manifest and metrics files; anything a run does not contain
// is shown as "not available" and nothing is computed in the browser. Reference strategies are
// listed apart from research runs and are always REFERENCE / NOT VALIDATED.

import { useState } from "react";

import { useBacktest, useBacktests } from "../api/hooks";
import type { BacktestSummary, Sourced } from "../api/types";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { ValidationBadge, validationStatus } from "../components/ValidationBadge";
import { NOT_AVAILABLE, formatUtc, pct, pick, shortHash, show } from "../format";

function winRate(run: BacktestSummary): string {
  if (run.win_rate === null) return NOT_AVAILABLE;
  const ci = run.win_rate_ci;
  return ci ? `${pct(run.win_rate)} [${pct(ci.lower)}, ${pct(ci.upper)}]` : pct(run.win_rate);
}

function isReference(run: BacktestSummary): boolean {
  return validationStatus({ strategyId: run.strategy_id }).tier === "REFERENCE";
}

function RunsTable({
  runs,
  selected,
  onSelect,
  testId,
}: {
  runs: Sourced<BacktestSummary>[];
  selected: string | null;
  onSelect: (id: string) => void;
  testId: string;
}) {
  return (
    <div className="table-wrap">
      <table className="table table--dense" data-testid={testId}>
        <thead>
          <tr>
            <th scope="col">Provenance</th>
            <th scope="col">Run</th>
            <th scope="col">Created</th>
            <th scope="col">Strategy</th>
            <th scope="col">Symbols</th>
            <th scope="col">Period</th>
            <th scope="col">Segment</th>
            <th scope="col">Validation</th>
            <th scope="col">Trades</th>
            <th scope="col">Win rate [CI]</th>
            <th scope="col">Metrics verified</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const run = r.value;
            if (run === null) return null;
            return (
              <tr key={run.run_id} className={run.run_id === selected ? "row--selected" : undefined}>
                <td>
                  <ProvenanceBadge provenance={r.provenance} reason={r.reason} compact />
                </td>
                <td>
                  <button
                    type="button"
                    className="link-button"
                    aria-pressed={run.run_id === selected}
                    onClick={() => onSelect(run.run_id)}
                  >
                    {run.run_id}
                  </button>
                </td>
                <td>{formatUtc(run.created_at)}</td>
                <td>
                  {show(run.strategy_id)} {run.strategy_version ? `v${run.strategy_version}` : ""}
                </td>
                <td>{run.symbols?.join(", ") ?? NOT_AVAILABLE}</td>
                <td>
                  {show(run.from)} → {show(run.to)}
                </td>
                <td>{show(run.segment)}</td>
                <td>
                  <ValidationBadge
                    strategyId={run.strategy_id}
                    grade={run.evidence_grade}
                    metricsVerified={run.metrics_verified}
                  />
                </td>
                <td className="num">{show(run.n_trades)}</td>
                <td className="num">{winRate(run)}</td>
                <td className={run.metrics_verified === false ? "bad" : undefined}>
                  {show(run.metrics_verified)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Kv({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <div key={k} className="kv__row">
          <dt>{k}</dt>
          <dd className={v === NOT_AVAILABLE ? "muted" : undefined}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const detail = useBacktest(runId);
  if (detail.isError) {
    return (
      <p className="bad" role="alert">
        Run {runId} could not be loaded: {String(detail.error)}
      </p>
    );
  }
  if (detail.data === undefined) return <p className="muted">Loading run {runId}…</p>;
  const summary = detail.data.summary.value;
  const manifest = detail.data.manifest.value ?? {};
  const metricsSourced = detail.data.metrics;
  const metrics = metricsSourced.value;
  const trades = pick(metrics, "trades");
  const reasons = pick(metrics, "grade_reasons");
  return (
    <section className="card" data-testid="backtest-detail" aria-label={`Run ${runId}`}>
      <h2 className="card__title">
        Run {runId} <ProvenanceBadge provenance={detail.data.summary.provenance} />{" "}
        <ValidationBadge
          strategyId={summary?.strategy_id}
          grade={summary?.evidence_grade}
          metricsVerified={summary?.metrics_verified ?? null}
        />
      </h2>
      {summary?.metrics_verified === false ? (
        <p className="bad" role="alert" data-testid="metrics-unverified">
          metrics.json does not match the SHA-256 recorded in the manifest: these metrics are not
          trustworthy.
        </p>
      ) : null}
      {metrics === null ? (
        <p className="bad" role="alert">
          Metrics unavailable: {metricsSourced.reason ?? "not returned"}.
        </p>
      ) : null}
      <h3>Reproducibility</h3>
      <Kv
        rows={[
          ["Strategy", `${show(summary?.strategy_id)} ${show(summary?.strategy_version)}`],
          ["Params hash", summary?.params_hash ? shortHash(summary.params_hash) : NOT_AVAILABLE],
          ["Symbols", summary?.symbols?.join(", ") ?? NOT_AVAILABLE],
          ["Period (UTC, end exclusive)", `${show(summary?.from)} → ${show(summary?.to)}`],
          ["Segment", show(summary?.segment)],
          ["Datasets", show(pick(manifest, "datasets"))],
          ["Cost model", show(pick(manifest, "cost_model"))],
          ["Funding", show(pick(manifest, "funding"))],
          ["Code version", show(pick(manifest, "code"))],
          ["Runner / simulator / features", `${show(pick(manifest, "runner_version"))} / ${show(pick(manifest, "sim_engine_version"))} / ${show(pick(manifest, "features_version"))}`],
          ["Config hash", show(pick(manifest, "config_hash"))],
          ["Seed (source)", `${show(pick(manifest, "seed"))} (${show(pick(manifest, "seed_source"))})`],
          ["Hypothesis / preregistration", `${show(pick(manifest, "hypothesis_id"))} / ${show(pick(manifest, "prereg_id"))}`],
          ["Manifest hash", show(pick(manifest, "manifest_hash"))],
          ["Trials in family (n_trials)", show(summary?.n_trials)],
          ["Recomputed with current trials", show(summary?.recomputed)],
          ["Metrics verified", show(summary?.metrics_verified)],
        ]}
      />
      <h3>Results (as recorded; not evidence of future performance)</h3>
      <Kv
        rows={[
          ["Trades", show(pick(trades, "n"))],
          ["Win rate", pct(pick(trades, "win_rate"))],
          [
            "Win rate CI",
            pick(trades, "win_rate_ci") === undefined || pick(trades, "win_rate_ci") === null
              ? NOT_AVAILABLE
              : `${pct(pick(trades, "win_rate_ci", "lower"))} – ${pct(pick(trades, "win_rate_ci", "upper"))}`,
          ],
          ["Expectancy (R, net of costs)", show(pick(trades, "expectancy_r"))],
          ["Expectancy (% of notional)", show(pick(trades, "expectancy_pct"))],
          ["Profit factor", show(pick(trades, "profit_factor"))],
          ["Total net PnL", show(pick(trades, "total_net_pnl"))],
          ["Fees", show(pick(trades, "total_fees"))],
          ["Slippage", show(pick(trades, "total_slippage"))],
          ["Cost share of gross", pct(pick(trades, "cost_share"))],
          ["Max drawdown (of peak equity)", pct(pick(metrics, "equity", "max_drawdown"))],
          ["Exit reasons", show(pick(trades, "exit_reasons"))],
          ["Ambiguous-bar share", pct(pick(trades, "ambiguous_share"))],
          ["Trades spanning gaps", pct(pick(trades, "spans_gap_share"))],
          ["Trades that waited for a traded candle", pct(pick(trades, "deferred_no_trade_share"))],
          ["Break-even win rate p*", show(summary?.p_star)],
          ["Margin lower bound", show(summary?.margin_lcb)],
          ["Signals (WATCH / CONFIRMED)", `${show(pick(metrics, "signals", "watch"))} / ${show(pick(metrics, "signals", "confirmed"))}`],
          ["Missing 1m bars in range", show(pick(metrics, "integrity", "missing_minutes_in_range"))],
          ["Exchange downtime minutes in range", show(pick(metrics, "integrity", "exchange_downtime_minutes_in_range"))],
          ["Zero-trade minute share", pct(pick(metrics, "integrity", "zero_trade_share"))],
        ]}
      />
      <h3>
        {summary?.recomputed
          ? "Grade reasons (recorded at run time; the grade shown was recomputed with the current trial count)"
          : "Grade reasons"}
      </h3>
      {Array.isArray(reasons) && reasons.length > 0 ? (
        <ul data-testid="grade-reasons">
          {reasons.map((r) => (
            <li key={String(r)}>{String(r)}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">{NOT_AVAILABLE}</p>
      )}
      <details>
        <summary>Cost stress, random-entry control and integrity (raw)</summary>
        <pre className="payload">
          {JSON.stringify(
            {
              cost_stress: pick(metrics, "cost_stress") ?? null,
              random_control: pick(metrics, "random_control") ?? null,
              integrity: pick(metrics, "integrity") ?? null,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </section>
  );
}

export function BacktestsPage() {
  const list = useBacktests();
  const [selected, setSelected] = useState<string | null>(null);
  const runs = list.data?.runs ?? [];
  const reference = runs.filter((r) => r.value !== null && isReference(r.value));
  const research = runs.filter((r) => r.value !== null && !isReference(r.value));

  return (
    <div className="page">
      <h1 className="page__title">Research › Backtests</h1>
      <p className="muted">
        Recorded runs (<code>BACKTEST</code>), read from each run's manifest and metrics. Grades follow
        the research protocol (A credible … X invalid); a backtest is not evidence of future
        performance, and only grade A counts as validated.
      </p>
      {list.isError ? (
        <p className="bad" role="alert">
          Backtests could not be loaded: {String(list.error)}
        </p>
      ) : list.data === undefined ? (
        <p className="muted">Loading…</p>
      ) : runs.length === 0 ? (
        <div className="empty" data-testid="backtests-empty">
          <p className="empty__title">No backtest runs recorded yet.</p>
          <p>
            Runs are written by <code>tsclaude backtest run</code> into <code>runs/</code>. Each run
            is recorded as a statistical trial of its strategy family, so runs are started
            deliberately, never from this page.
          </p>
        </div>
      ) : (
        <>
          <h2>Research runs</h2>
          {research.length === 0 ? (
            <p className="muted" data-testid="research-empty">
              No research runs recorded.
            </p>
          ) : (
            <RunsTable runs={research} selected={selected} onSelect={setSelected} testId="research-runs" />
          )}
          <h2>
            Reference runs <ValidationBadge label="REFERENCE" />
          </h2>
          {reference.length === 0 ? (
            <p className="muted">No reference runs recorded.</p>
          ) : (
            <RunsTable runs={reference} selected={selected} onSelect={setSelected} testId="reference-runs" />
          )}
          {selected !== null ? <RunDetail runId={selected} /> : null}
        </>
      )}
    </div>
  );
}
