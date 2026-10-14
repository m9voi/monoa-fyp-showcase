// Display-only money formatting for the Finance UI.
// The backend is authoritative for every value (computed in integer sen);
// the frontend only formats what it is given.

export function formatMoney(value: number | null | undefined): string {
  const n = typeof value === "number" && Number.isFinite(value) ? value : 0;
  const sign = n < 0 ? "-" : "";
  return (
    sign +
    Math.abs(n).toLocaleString("en-MY", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

/** "RM 1,234.56" (handles negatives correctly, unlike the old floor/%1 hack). */
export function rm(value: number | null | undefined): string {
  return `RM ${formatMoney(value)}`;
}

/** Clamp a raw percentage to 0..100 for progress bars (keep the real number for labels). */
export function clampPct(pct: number | null | undefined): number {
  const n = typeof pct === "number" && Number.isFinite(pct) ? pct : 0;
  return Math.max(0, Math.min(100, n));
}
