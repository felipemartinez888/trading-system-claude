// Live › Markets: watchlist with every value's provenance, and a 1m chart for the selected symbol.

import { useState } from "react";

import { useMarkets } from "../api/hooks";
import type { Market, MarketRow, Sourced, StreamState } from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { type Freshness, shown, useBackendFreshness } from "../freshness";

export function formatPrice(v: number): string {
  if (!Number.isFinite(v)) return "—";
  const digits = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function formatPct(v: number, digits = 2): string {
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;
}

/** A value cell: formatted value (or a dash) plus the API-provided provenance badge. */
function Cell<T>({
  sourced,
  render,
  freshness,
  testId,
}: {
  sourced: Sourced<T>;
  render: (v: T) => string;
  freshness: Freshness;
  testId?: string;
}) {
  const display = shown(sourced, freshness);
  const value = sourced.value;
  return (
    <td className="num cell" data-testid={testId} data-provenance={display.provenance}>
      <span className={display.provenance === "STALE" ? "muted" : undefined}>
        {value === null ? "—" : render(value)}
      </span>{" "}
      <ProvenanceBadge {...display} compact />
    </td>
  );
}

function FreshnessDot({ state, freshness }: { state: Sourced<StreamState>; freshness: Freshness }) {
  const display = shown(state, freshness);
  const value = state.value;
  const ok = value === "HEALTHY" && display.provenance === "SYSTEM";
  return (
    <td className="cell" data-testid="freshness">
      <span className={`fdot ${ok ? "fdot--ok" : "fdot--bad"}`} aria-hidden="true" />{" "}
      {value ?? "—"} <ProvenanceBadge {...display} compact />
    </td>
  );
}

function spreadBps(row: MarketRow): Sourced<number> {
  const book = row.best_bid_ask;
  if (book.value === null) return { value: null, provenance: book.provenance, as_of_ms: book.as_of_ms, reason: book.reason };
  const mid = (book.value.bid_price + book.value.ask_price) / 2;
  const bps = mid > 0 ? ((book.value.ask_price - book.value.bid_price) / mid) * 10_000 : Number.NaN;
  // Same observation as the book, so the same provenance (no relabelling).
  return { ...book, value: bps };
}

export function MarketsTable({
  rows,
  selected,
  onSelect,
}: {
  rows: MarketRow[];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const freshness = useBackendFreshness(); // once per table, not per cell
  return (
    <table className="table" data-testid="markets-table">
      <thead>
        <tr>
          <th scope="col">Market</th>
          <th scope="col">Symbol</th>
          <th scope="col">Last close (1m)</th>
          <th scope="col">Forming</th>
          <th scope="col">Bid / Ask</th>
          <th scope="col">Spread</th>
          <th scope="col">Mark</th>
          <th scope="col">Funding</th>
          <th scope="col">24h</th>
          <th scope="col">Freshness</th>
          <th scope="col">Open gaps</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const key = `${r.market}:${r.symbol}`;
          return (
            <tr
              key={key}
              className={key === selected ? "row--selected" : undefined}
              data-testid={`market-row-${key}`}
            >
              <td>{r.market === "spot" ? "Spot" : "Perp"}</td>
              <td>
                <button type="button" className="link-button" onClick={() => onSelect(key)} aria-pressed={key === selected}>
                  {r.symbol}
                </button>
              </td>
              <Cell freshness={freshness} sourced={r.last_closed_bar} render={(b) => formatPrice(b.close)} testId="last-close" />
              <Cell freshness={freshness} sourced={r.partial_candle} render={(c) => formatPrice(c.close)} testId="partial" />
              <Cell
                freshness={freshness}
                sourced={r.best_bid_ask}
                render={(b) => `${formatPrice(b.bid_price)} / ${formatPrice(b.ask_price)}`}
                testId="book"
              />
              <Cell freshness={freshness} sourced={spreadBps(r)} render={(v) => `${v.toFixed(2)} bp`} testId="spread" />
              <Cell freshness={freshness} sourced={r.mark_price} render={formatPrice} testId="mark" />
              <Cell freshness={freshness} sourced={r.funding} render={(f) => formatPct(f.rate, 4)} testId="funding" />
              <Cell freshness={freshness} sourced={r.change_24h} render={(v) => formatPct(v)} testId="change-24h" />
              <FreshnessDot state={r.freshness} freshness={freshness} />
              <Cell freshness={freshness} sourced={r.open_gap_minutes} render={(v) => (v === 0 ? "0" : `${v} min`)} testId="gaps" />
              <Cell freshness={freshness} sourced={r.exchange_status} render={(s) => s} testId="status" />
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function MarketsPage() {
  const markets = useMarkets();
  const [selected, setSelected] = useState<string | null>(null);
  const rows = markets.data?.rows ?? [];
  const key = selected ?? (rows[0] ? `${rows[0].market}:${rows[0].symbol}` : null);
  const row = rows.find((r) => `${r.market}:${r.symbol}` === key);
  const [market, symbol] = (key ?? ":").split(":") as [Market, string];

  return (
    <div className="page">
      <h1 className="page__title">Live › Markets</h1>
      {markets.isError ? (
        <p className="bad" role="alert">
          Market data could not be loaded: {String(markets.error)}
        </p>
      ) : markets.data === undefined ? (
        <p className="muted">Loading…</p>
      ) : rows.length === 0 ? (
        <section className="empty" data-testid="markets-empty">
          <h2 className="empty__title">No market data yet</h2>
          <p>
            Monitoring has not been started in this app session. Press <strong>Start</strong> in
            the status bar: the watchlist fills with Binance Spot and USDⓈ-M values for the
            configured symbols, each labeled with its provenance.
          </p>
        </section>
      ) : (
        <>
          {markets.data.running.value === false ? (
            <p className="statusbar__notice statusbar__notice--warn" role="status">
              Monitoring is stopped: the values below are the last known ones (STALE).
            </p>
          ) : null}
          <MarketsTable rows={rows} selected={key} onSelect={setSelected} />
          {key !== null ? <CandleChart key={key} market={market} symbol={symbol} row={row} /> : null}
        </>
      )}
    </div>
  );
}
