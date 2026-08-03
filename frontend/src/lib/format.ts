/** API timestamps are always UTC (see backend's _utcnow), but SQLite's
 * DateTime(timezone=True) isn't enforced by the driver, so they can round-trip as
 * offset-naive ISO strings - `new Date(...)` would then parse them as local time.
 * Same bug class already fixed backend-side (worker/loop.py, benchmarks/report.py). */
export function parseApiDate(value: string): Date {
  const hasOffset = /Z$|[+-]\d{2}:\d{2}$/.test(value);
  return new Date(hasOffset ? value : `${value}Z`);
}

/** Renders `null`/`undefined` as "N/A" rather than "0" - the distinction matters
 * here: metrics like recall/precision are `null` when there's no actual_label data
 * yet (see comparison endpoint docs), not when the value happens to be zero. */
export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "N/A";
  return value.toFixed(digits);
}

export function formatMs(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "N/A";
  return `${value.toFixed(1)} ms`;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "N/A";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = parseApiDate(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}
