// Validation status of a strategy, run or signal, derived only from fields the backend returns.
// Order of the rules:
// 1. a REFERENCE strategy is always "REFERENCE / NOT VALIDATED", whatever grade it carries;
// 2. a backtest grade read from metrics that failed their manifest hash check is not trusted
//    (an integrity rule, not a grading rule: CLAUDE.md fail closed / truthful status);
// 3. otherwise the grade is shown with its RESEARCH_PROTOCOL §6 meaning, word for word. Only
//    grade A ("credible") is called validated; no other standard is introduced here.

/** Pipeline strategies that are never research evidence (MVP-3: paper only, not validated). */
export const REFERENCE_STRATEGIES: readonly string[] = ["bracket_reference"];

export type ValidationTier =
  | "REFERENCE"
  | "UNVERIFIED"
  | "VALIDATED"
  | "CANDIDATE"
  | "INSUFFICIENT"
  | "REJECTED"
  | "INVALID"
  | "UNGRADED";

export interface ValidationStatus {
  tier: ValidationTier;
  text: string;
  description: string;
}

export interface ValidationInput {
  strategyId?: string | null | undefined;
  label?: string | null | undefined;
  grade?: string | null | undefined;
  /** Backtests only: `metrics_verified` from the API. Omit for items without a metrics file. */
  metricsVerified?: boolean | null | undefined;
}

const NOT_VALIDATED = " Not validated.";

export function validationStatus({
  strategyId,
  label,
  grade,
  metricsVerified,
}: ValidationInput): ValidationStatus {
  if (label === "REFERENCE" || (strategyId != null && REFERENCE_STRATEGIES.includes(strategyId))) {
    return {
      tier: "REFERENCE",
      text: "REFERENCE / NOT VALIDATED",
      description:
        "Pipeline reference strategy: not claimed profitable, not research evidence, paper only. " +
        "Any grade it carries is not a validation result.",
    };
  }
  if (metricsVerified !== undefined && metricsVerified !== true) {
    return {
      tier: "UNVERIFIED",
      text: `UNVERIFIED (recorded grade ${grade ?? "none"})`,
      description:
        "metrics.json does not match the SHA-256 recorded in the run manifest (or could not be " +
        "checked), so its grade is not trusted.",
    };
  }
  switch (grade) {
    case "A":
      return {
        tier: "VALIDATED",
        text: "VALIDATED (A – credible)",
        description:
          "Passed pre-registered criteria on validation and holdout; robust to cost stress.",
      };
    case "B":
      return {
        tier: "CANDIDATE",
        text: "RESEARCH / CANDIDATE (B – promising)",
        description: "Passed validation; holdout pending or robustness caveats." + NOT_VALIDATED,
      };
    case "C":
      return {
        tier: "INSUFFICIENT",
        text: "INSUFFICIENT (C – insufficient)",
        description: "Too few trades, wide CIs, or high ambiguous-bar share." + NOT_VALIDATED,
      };
    case "D":
      return { tier: "REJECTED", text: "REJECTED (D – rejected)", description: "Failed rejection criteria." };
    case "X":
      return {
        tier: "INVALID",
        text: "INVALID (X – invalid)",
        description:
          "Integrity failure: data gaps in range, look-ahead canary failure, missing manifest fields.",
      };
    default:
      return {
        tier: "UNGRADED",
        text: "NOT GRADED",
        description: "No evidence grade was returned for this item.",
      };
  }
}

export function ValidationBadge(props: ValidationInput) {
  const status = validationStatus(props);
  return (
    <span
      className={`vbadge vbadge--${status.tier.toLowerCase()}`}
      data-tier={status.tier}
      title={status.description}
      aria-label={`validation ${status.text}: ${status.description}`}
      role="img"
    >
      {status.text}
    </span>
  );
}
