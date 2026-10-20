import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PROVENANCES } from "../api/types";
import { DemoWatermark, PROVENANCE_META, ProvenanceBadge } from "./ProvenanceBadge";

describe("ProvenanceBadge", () => {
  it("covers every Provenance value in the contract", () => {
    expect(Object.keys(PROVENANCE_META).sort()).toEqual([...PROVENANCES].sort());
  });

  it.each(PROVENANCES)("renders %s with its label and data attribute", (provenance) => {
    render(<ProvenanceBadge provenance={provenance} />);
    const badge = screen.getByRole("img", { name: new RegExp(`provenance ${provenance}`) });
    expect(badge).toHaveTextContent(provenance);
    expect(badge).toHaveAttribute("data-provenance", provenance);
    expect(badge).toHaveClass(`prov--${provenance.toLowerCase()}`);
    if (provenance === "DEMO") {
      expect(badge).toHaveClass("prov--hatched");
    } else {
      expect(badge).not.toHaveClass("prov--hatched");
    }
  });

  it("includes the reason and as-of time in the accessible description", () => {
    render(
      <ProvenanceBadge
        provenance="UNAVAILABLE"
        reason="not implemented until M1"
        asOfMs={Date.parse("2026-10-20T12:00:00.000Z")}
      />,
    );
    const badge = screen.getByRole("img");
    expect(badge.getAttribute("title")).toContain("not implemented until M1");
    expect(badge.getAttribute("title")).toContain("2026-10-20T12:00:00.000Z");
  });

  it("wraps demo content with a hatched watermark and banner", () => {
    render(
      <DemoWatermark>
        <span>example</span>
      </DemoWatermark>,
    );
    expect(screen.getByTestId("demo-watermark")).toHaveClass("demo-watermark");
    expect(screen.getByRole("note")).toHaveTextContent(/DEMO/);
  });
});
