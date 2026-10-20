import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom has no canvas: replace the chart library with an inert recorder for every test.
vi.mock("lightweight-charts", () => {
  const series = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  return {
    ColorType: { Solid: "solid" },
    CandlestickSeries: "Candlestick",
    HistogramSeries: "Histogram",
    createChart: vi.fn(() => ({
      addSeries: vi.fn(series),
      priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
      remove: vi.fn(),
    })),
  };
});

afterEach(() => {
  cleanup();
  document.head.querySelectorAll('meta[name="tsc-token"]').forEach((m) => m.remove());
  window.localStorage.clear();
});
