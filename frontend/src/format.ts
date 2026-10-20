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
