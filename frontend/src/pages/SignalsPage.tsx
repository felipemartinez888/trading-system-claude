// Live › Signals: inbox of demo-detector WATCH events (M1). Everything here is DEMO: the detector
// only exercises the live pipeline; it has no edge and its events are not recommendations.

import { useSignals } from "../api/hooks";
import { DemoWatermark, ProvenanceBadge } from "../components/ProvenanceBadge";
import { formatUtc } from "../format";
import { formatPct, formatPrice } from "./MarketsPage";

export function SignalsPage() {
  const signals = useSignals();
  const data = signals.data?.signals;
  const items = data?.value ?? [];

  return (
    <div className="page">
      <h1 className="page__title">Live › Signals</h1>
      <DemoWatermark>
        <p>
          <strong>DEMO detector, not a strategy.</strong> It emits a <code>WATCH</code> when a
          live-closed 1m bar's body exceeds a threshold, only while every stream of that symbol is
          HEALTHY. Never <code>CONFIRMED</code>, never sized, never routed to a paper book. Real,
          pre-registered strategies replace it in M4.
        </p>
        {signals.isError ? (
          <p className="bad" role="alert">
            Signals could not be loaded: {String(signals.error)}
          </p>
        ) : data === undefined ? (
          <p className="muted">Loading…</p>
        ) : items.length === 0 ? (
          <p data-testid="signals-empty">
            No demo WATCH events yet. They appear while monitoring runs, when a live 1m bar moves
            more than the threshold.
          </p>
        ) : (
          <table className="table" data-testid="signals-table">
            <thead>
              <tr>
                <th scope="col">Provenance</th>
                <th scope="col">Decision (bar close, UTC)</th>
                <th scope="col">Market</th>
                <th scope="col">Symbol</th>
                <th scope="col">Stage</th>
                <th scope="col">Side</th>
                <th scope="col">Ref. price</th>
                <th scope="col">1m body</th>
                <th scope="col">Grade</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.signal_id}>
                  <td>
                    <ProvenanceBadge provenance={data.provenance} reason={data.reason} />
                  </td>
                  <td>{formatUtc(s.decision_time)}</td>
                  <td>{s.market === "spot" ? "Spot" : "Perp"}</td>
                  <td>{s.symbol}</td>
                  <td>{s.stage}</td>
                  <td>{s.side}</td>
                  <td className="num">{formatPrice(s.ref_price)}</td>
                  <td className="num" title={`threshold ±${formatPct(s.threshold)}`}>
                    {formatPct(s.move)}
                  </td>
                  <td>{s.evidence_grade}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </DemoWatermark>
    </div>
  );
}
