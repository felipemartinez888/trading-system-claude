// 1m candlestick chart (TradingView Lightweight Charts™, Apache-2.0) with gap shading and a source
// legend. Candles are coloured by *source* where it matters: live-closed bars use the up/down
// palette; REST-recovered and backfilled bars have their own colours; the forming candle is hollow.
// Missing minutes (open gaps) are shaded; nothing is ever drawn for a minute we do not have.

import {
  CandlestickSeries,
  ColorType,
  type CandlestickData,
  createChart,
  HistogramSeries,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";

import { useBars, useGaps } from "../api/hooks";
import type { BarOut, GapOut, Market, MarketRow } from "../api/types";
import { ProvenanceBadge } from "./ProvenanceBadge";

// Okabe–Ito palette, matching the CSS variables in styles.css.
export const SOURCE_COLORS = {
  up: "#56b4e9",
  down: "#e69f00",
  rest_recovered: "#cc79a7",
  rest_backfill: "#8a93a6",
  partial: "#f0e442",
  gap: "rgba(213, 94, 0, 0.28)",
} as const;

const MINUTE = 60_000;

const toTime = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;

export function candleOf(bar: BarOut): CandlestickData<UTCTimestamp> {
  const base = { time: toTime(bar.open_time_ms), open: bar.open, high: bar.high, low: bar.low, close: bar.close };
  if (bar.source === "ws_live") return base; // series up/down colours
  const color = bar.source === "rest_recovered" ? SOURCE_COLORS.rest_recovered : SOURCE_COLORS.rest_backfill;
  return { ...base, color, borderColor: color, wickColor: color };
}

/** Missing 1m open times inside [from, to) for this symbol, from the open gaps. */
export function gapMinutes(gaps: GapOut[], market: Market, symbol: string, from: number, to: number): number[] {
  const out: number[] = [];
  for (const g of gaps) {
    if (g.market !== market || g.symbol !== symbol) continue;
    for (const [lo, hi] of g.missing_ranges_ms) {
      for (let t = Math.max(lo, from); t < Math.min(hi, to); t += MINUTE) out.push(t);
    }
  }
  return [...new Set(out)].sort((a, b) => a - b);
}

export interface CandleChartProps {
  market: Market;
  symbol: string;
  row: MarketRow | undefined;
}

export function CandleChart({ market, symbol, row }: CandleChartProps) {
  const bars = useBars(market, symbol);
  const gaps = useGaps();
  const container = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const gapsRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  const items = useMemo(() => bars.data?.bars.value ?? [], [bars.data]);
  const partial = row?.partial_candle;

  useEffect(() => {
    if (container.current === null) return;
    const chart = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#0f1115" },
        textColor: "#a3aab8",
        attributionLogo: true, // required attribution (TradingView logo link)
      },
      grid: { vertLines: { color: "#1c2130" }, horzLines: { color: "#1c2130" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: "#2a3040" },
    });
    candlesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: SOURCE_COLORS.up,
      downColor: SOURCE_COLORS.down,
      borderUpColor: SOURCE_COLORS.up,
      borderDownColor: SOURCE_COLORS.down,
      wickUpColor: SOURCE_COLORS.up,
      wickDownColor: SOURCE_COLORS.down,
    });
    gapsRef.current = chart.addSeries(HistogramSeries, {
      priceScaleId: "gaps",
      color: SOURCE_COLORS.gap,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale("gaps").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      gapsRef.current = null;
    };
  }, []);

  useEffect(() => {
    const series = candlesRef.current;
    if (series === null) return;
    const data = items.map(candleOf);
    const last = items.at(-1);
    const forming = partial?.value ?? null;
    if (forming !== null && (last === undefined || forming.open_time_ms > last.open_time_ms)) {
      data.push({
        time: toTime(forming.open_time_ms),
        open: forming.open,
        high: forming.high,
        low: forming.low,
        close: forming.close,
        color: "rgba(0, 0, 0, 0)",
        borderColor: SOURCE_COLORS.partial,
        wickColor: SOURCE_COLORS.partial,
      });
    }
    series.setData(data);
  }, [items, partial]);

  useEffect(() => {
    const series = gapsRef.current;
    if (series === null || bars.data === undefined) return;
    const minutes = gapMinutes(
      gaps.data?.gaps.value ?? [],
      market,
      symbol,
      bars.data.from_ms,
      bars.data.to_ms,
    );
    const data: HistogramData<UTCTimestamp>[] = minutes.map((t) => ({ time: toTime(t), value: 1 }));
    series.setData(data);
  }, [gaps.data, bars.data, market, symbol]);

  return (
    <section className="card" aria-labelledby="chart-title">
      <h2 id="chart-title" className="card__title">
        {market === "spot" ? "Spot" : "USDⓈ-M perp"} {symbol} · 1m{" "}
        {bars.data ? <ProvenanceBadge provenance={bars.data.bars.provenance} reason={bars.data.bars.reason} compact /> : null}
      </h2>
      {bars.isError ? (
        <p className="bad" role="alert">
          Bars could not be loaded: {String(bars.error)}
        </p>
      ) : bars.data !== undefined && items.length === 0 && partial?.value == null ? (
        <p className="muted" data-testid="chart-empty">
          No stored 1m bars for the last 6 hours. Bars appear after monitoring starts (startup
          backfill, then live closes).
        </p>
      ) : null}
      <div ref={container} className="chart" data-testid="candle-chart" />
      <ul className="legend" aria-label="Chart legend">
        <li>
          <span className="swatch swatch--up" /> <span className="swatch swatch--down" /> closed live
          (WebSocket)
        </li>
        <li>
          <span className="swatch swatch--recovered" /> recovered via REST
        </li>
        <li>
          <span className="swatch swatch--backfill" /> backfill (not observed live)
        </li>
        <li>
          <span className="swatch swatch--partial" /> forming (PARTIAL)
        </li>
        <li>
          <span className="swatch swatch--gap" /> missing minutes (open gap)
        </li>
      </ul>
      <p className="attribution">
        Charts by{" "}
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">
          TradingView
        </a>{" "}
        Lightweight Charts™ (Apache-2.0).
      </p>
    </section>
  );
}
