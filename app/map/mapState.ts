// Phase 5D — pure map-state helpers (no DOM, no Leaflet). Unit-tested with
// `npm run test:node`. Keeping the selection/bounds/directions logic here means
// the Map page and the map canvas share ONE definition and it cannot drift.

import type { CorePlace } from "../lib/types";
import { safeExternalHref } from "../lib/safeUrl.ts";

export interface LatLng {
  latitude: number;
  longitude: number;
}

/**
 * Reconcile a selection against a fresh result set: keep the selected id only if
 * that place is still present, otherwise clear it. Prevents a stale place from
 * staying "selected" after a new search / activity switch.
 */
export function reconcileSelection(
  selectedId: string | null,
  places: readonly CorePlace[],
): string | null {
  if (selectedId === null) return null;
  return places.some((p) => p.place_id === selectedId) ? selectedId : null;
}

/**
 * Bounds `[[south, west], [north, east]]` covering the user position (if known)
 * and every place. Returns null when there is nothing to frame.
 */
export function resultBounds(
  user: LatLng | null,
  places: readonly CorePlace[],
): [[number, number], [number, number]] | null {
  const pts: Array<[number, number]> = [];
  if (user && Number.isFinite(user.latitude) && Number.isFinite(user.longitude)) {
    pts.push([user.latitude, user.longitude]);
  }
  for (const p of places) {
    if (Number.isFinite(p.latitude) && Number.isFinite(p.longitude)) {
      pts.push([p.latitude, p.longitude]);
    }
  }
  if (pts.length === 0) return null;
  let south = pts[0][0];
  let north = pts[0][0];
  let west = pts[0][1];
  let east = pts[0][1];
  for (const [lat, lng] of pts) {
    south = Math.min(south, lat);
    north = Math.max(north, lat);
    west = Math.min(west, lng);
    east = Math.max(east, lng);
  }
  return [[south, west], [north, east]];
}

/**
 * External directions URL for a place. Prefers the provider-supplied maps URL
 * (Phase 5C `maps_url` — e.g. Google's `googleMapsUri`); otherwise a safe
 * coordinate/name Google Maps search link. NEVER contains an API key.
 *
 * Phase 5H-B6: the provider value is validated through the single external-URL
 * allow-list (`safeExternalHref`, http/https + host only) rather than a local
 * `startsWith` check, so every external anchor in the app shares one gate.
 */
export function directionsUrl(place: CorePlace): string {
  const provided = safeExternalHref(place.maps_url);
  if (provided) return provided;
  const q =
    Number.isFinite(place.latitude) && Number.isFinite(place.longitude)
      ? `${place.latitude},${place.longitude}`
      : encodeURIComponent(place.name);
  return `https://www.google.com/maps/search/?api=1&query=${q}`;
}

/** Distance label — straight line only, never implies road distance. */
export function formatDistance(meters: number): string {
  if (!Number.isFinite(meters) || meters < 0) return "";
  return meters < 950
    ? `${Math.round(meters / 10) * 10} m away`
    : `${(meters / 1000).toFixed(1)} km away`;
}

/**
 * Distance text for a place. When the user's location fix is too coarse
 * (`reliable === false`) a metre-level figure would mislead, so say so instead.
 * The list is still ordered by the computed distance — only the displayed
 * number is withheld.
 */
export function distanceLabel(meters: number, reliable: boolean): string {
  if (!reliable) return "distance not reliable";
  return formatDistance(meters);
}
