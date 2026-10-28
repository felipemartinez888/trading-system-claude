import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ValidationBadge, validationStatus } from "./ValidationBadge";

describe("validation status", () => {
  it("always shows a reference strategy as REFERENCE / NOT VALIDATED, whatever its grade", () => {
    for (const grade of ["A", "B", "C", "D", "X", null]) {
      expect(validationStatus({ strategyId: "bracket_reference", grade }).text).toBe(
        "REFERENCE / NOT VALIDATED",
      );
      expect(validationStatus({ label: "REFERENCE", grade, metricsVerified: false }).tier).toBe(
        "REFERENCE",
      );
    }
  });

  it("uses the RESEARCH_PROTOCOL §6 meaning of each grade, word for word", () => {
    const of = (grade: string | null) => validationStatus({ strategyId: "h1", grade });
    expect(of("A").tier).toBe("VALIDATED");
    expect(of("A").text).toBe("VALIDATED (A – credible)");
    expect(of("B").text).toBe("RESEARCH / CANDIDATE (B – promising)");
    expect(of("C").tier).toBe("INSUFFICIENT");
    expect(of("C").text).toBe("INSUFFICIENT (C – insufficient)");
    expect(of("C").description).toContain("Too few trades, wide CIs, or high ambiguous-bar share.");
    expect(of("D").tier).toBe("REJECTED");
    expect(of("X").tier).toBe("INVALID");
    expect(of(null).text).toBe("NOT GRADED");
    expect(of("Z").tier).toBe("UNGRADED");
  });

  it("never trusts a grade from metrics that failed their hash check", () => {
    for (const metricsVerified of [false, null]) {
      const status = validationStatus({ strategyId: "h1", grade: "A", metricsVerified });
      expect(status.tier).toBe("UNVERIFIED");
      expect(status.text).toBe("UNVERIFIED (recorded grade A)");
    }
    expect(validationStatus({ strategyId: "h1", grade: "A", metricsVerified: true }).tier).toBe(
      "VALIDATED",
    );
  });

  it("renders an accessible badge with the explanation", () => {
    render(<ValidationBadge strategyId="bracket_reference" grade="X" />);
    const badge = screen.getByRole("img", { name: /REFERENCE \/ NOT VALIDATED/ });
    expect(badge).toHaveAttribute("data-tier", "REFERENCE");
    expect(badge.getAttribute("title")).toMatch(/not research evidence/);
  });
});
