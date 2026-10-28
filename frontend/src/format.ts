// Display helpers. Times come from the API as ISO-8601 UTC ("...Z") and are shown in UTC.

export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "Z");
}

export function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const s = Math.floor(totalSeconds);
  const days = Math.floor(s / 86_400);
  const hours = Math.floor((s % 86_400) / 3_600);
  const minutes = Math.floor((s % 3_600) / 60);
  const seconds = s % 60;
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** Duration between two ISO instants; `end = null` means "until now". */
export function durationBetween(startIso: string, endIso: string | null, nowMs = Date.now()): string {
  const start = Date.parse(startIso);
  const end = endIso === null ? nowMs : Date.parse(endIso);
  if (Number.isNaN(start) || Number.isNaN(end)) return "—";
  return formatDuration((end - start) / 1000);
}

export function shortHash(hash: string, size = 12): string {
  return hash.length > size ? `${hash.slice(0, size)}…` : hash;
}

// ---------------------------------------------------------------------- MVP-4 value display
// Formatting only: nothing here derives a metric. Absent values are said to be absent.

export const NOT_AVAILABLE = "not available";

/** Read a nested field of an untyped backend object; undefined when any step is missing. */
export function pick(obj: unknown, ...path: string[]): unknown {
  let cur: unknown = obj;
  for (const key of path) {
    if (cur === null || typeof cur !== "object" || !(key in cur)) return undefined;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur;
}

/** A backend value as text; small numbers keep their significant digits (never "0.0000"). */
export function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return NOT_AVAILABLE;
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(6)));
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** A fraction (0.0123) shown as a percentage; numeric strings (exact Decimals) are accepted. */
export function pct(value: unknown): string {
  const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : NOT_AVAILABLE;
}

/** Epoch milliseconds as UTC text. */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return NOT_AVAILABLE;
  return new Date(ms).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "Z");
}
