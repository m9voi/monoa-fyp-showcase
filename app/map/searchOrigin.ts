// Phase 5D — the explicit search-origin model. Pure, DOM-free, unit-tested.
//
// The Map has ONE search origin at a time. It is either:
//   * "device" — the validated Phase 5B geolocation fix (with its accuracy), or
//   * "manual" — an area the user explicitly picked from a geocoding result.
// These are never conflated. Every place search, distance, marker and label
// reads from this one value.

import { isAccuracyUnreliable } from "../lib/location.ts";

export type SearchOrigin =
  | { source: "device"; latitude: number; longitude: number; accuracyMeters: number | null }
  | { source: "manual"; latitude: number; longitude: number; label: string };

/** Short label for the origin chip. */
export function originLabel(o: SearchOrigin): string {
  return o.source === "manual" ? o.label : "Your location";
}

/**
 * May CoreOS state a confident metre-level distance from this origin?
 *  - manual: yes — the user picked a definite point;
 *  - device: yes, unless the reported accuracy is so coarse the distance would
 *    mislead (Phase 5D "very approximate" tier).
 */
export function originDistanceReliable(o: SearchOrigin): boolean {
  return o.source === "manual" || !isAccuracyUnreliable(o.accuracyMeters);
}

/** True when the origin is a device fix flagged "very approximate". */
export function originIsVeryApproximate(o: SearchOrigin | null): boolean {
  return o != null && o.source === "device" && isAccuracyUnreliable(o.accuracyMeters);
}
