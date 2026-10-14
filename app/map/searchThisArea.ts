// B10 — pure helpers for the Map "Search this area" affordance and the adaptive
// (8 -> 12 -> 20 km) expanded-radius note. No DOM, no Leaflet, no React. Keeping
// the visibility / distance logic here means the Map page and its tests share
// ONE definition and it cannot drift. Unit-tested with `npm run test:node`.

export interface LatLng {
  latitude: number;
  longitude: number;
}

// A user must pan/zoom the map at least this far (metres) from the centre of the
// last COMPLETED search before "Search this area" appears. Small enough to feel
// responsive, large enough that ordinary map jitter / a tap-nudge / a
// programmatic recentre never surfaces the control.
export const AREA_SEARCH_MIN_MOVE_M = 750;

const EARTH_RADIUS_M = 6_371_000;

/** Great-circle straight-line distance in metres (mirrors places_core.haversine_m). */
export function haversineMeters(a: LatLng, b: LatLng): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.latitude - a.latitude);
  const dLng = toRad(b.longitude - a.longitude);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.latitude)) * Math.cos(toRad(b.latitude)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

function finite(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/**
 * Show "Search this area" ONLY when the current map centre has moved a
 * meaningful distance away from the centre of the last completed search.
 * Returns false when either point is missing (no search yet / centre unknown)
 * or non-finite, and false for sub-threshold jitter.
 */
export function shouldOfferAreaSearch(
  lastSearchCenter: LatLng | null | undefined,
  mapCenter: LatLng | null | undefined,
  minMoveM: number = AREA_SEARCH_MIN_MOVE_M,
): boolean {
  if (!lastSearchCenter || !mapCenter) return false;
  if (
    !finite(lastSearchCenter.latitude) ||
    !finite(lastSearchCenter.longitude) ||
    !finite(mapCenter.latitude) ||
    !finite(mapCenter.longitude)
  ) {
    return false;
  }
  return haversineMeters(lastSearchCenter, mapCenter) > minMoveM;
}

// The base rung of the adaptive ladder. A settled radius at or below this needs
// no note; a wider one gets a concise factual line (never the technical ladder).
export const ADAPTIVE_BASE_RADIUS_M = 8_000;

/** "Expanded search to 12 km" / "Expanded search to 20 km", or null at/below base
 *  or for a missing/invalid value. Factual — not a control, not weather. */
export function expandedRadiusNote(radiusM: unknown): string | null {
  if (!finite(radiusM)) return null;
  if (radiusM <= ADAPTIVE_BASE_RADIUS_M) return null;
  return `Expanded search to ${Math.round(radiusM / 1000)} km`;
}
