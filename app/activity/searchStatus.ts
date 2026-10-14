// B9 — Activity adaptive nearby search: tiny, pure presentation helpers.
//
// The 8 / 12 / 20 km ladder is decided SERVER-side and never exposed as a
// control. The client only shows a short, factual note when the search had to
// widen past the 8 km base radius. No radius picker, no filter panel.

/** The metres value below which no "expanded" note is shown (the base search). */
export const BASE_RADIUS_M = 8_000;

/**
 * A one-line contextual note when the adaptive search widened past 8 km, else
 * null. `radiusM` is `ActivitySearchResponse.radius_m` — the radius the server
 * settled on. Anything at or below the base radius, and any missing / invalid
 * value, yields null (show nothing rather than a wrong claim).
 */
export function expandedRadiusNote(radiusM: unknown): string | null {
  if (typeof radiusM !== "number" || !Number.isFinite(radiusM)) return null;
  if (radiusM <= BASE_RADIUS_M) return null;
  const km = Math.round(radiusM / 1000);
  return `Expanded search to ${km} km`;
}
