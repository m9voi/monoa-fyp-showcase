// Phase 5G — pure helpers for the Schedule <-> Map bridge (no DOM, no React).
// Unit-tested with `npm run test:node`. Keeping the URL / identity logic here
// means the Schedule detail sheet and the Map focus view share ONE definition
// and it cannot drift.
//
// TWO hard rules encoded here:
//   1. A linked place is carried by STABLE IDENTITY (provider + provider_place_id
//      + a name/coords snapshot) — never by a bare display string.
//   2. A handoff URL may carry the PLACE's public identity, but NEVER the current
//      user's origin / device coordinates.

import type { CorePlace, EventPlaceRef, PlaceResolveResponse } from "../lib/types";

// Where a Map "View place" handoff came from. The URL contract is otherwise
// identical for both — same `focus_*` params, still NO user origin / device
// coordinates — this only tells the Map focus view which surface to send the
// user "Back" to and how to label the context.
export type FocusSource = "schedule" | "activity";

const LAT_MIN = -90;
const LAT_MAX = 90;
const LNG_MIN = -180;
const LNG_MAX = 180;

const GOOGLE_ID_RE = /^[A-Za-z0-9_-]{4,128}$/;
const OSM_ID_RE = /^(node|way|relation):[0-9]{1,20}$/;

function finite(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

/** Narrow any place-shaped object to the STABLE reference we persist / hand off. */
export function toPlaceRef(
  p: Pick<CorePlace, "provider" | "provider_place_id" | "name" | "latitude" | "longitude"> &
    Partial<Pick<CorePlace, "address">>,
): EventPlaceRef {
  return {
    provider: p.provider,
    provider_place_id: p.provider_place_id,
    place_id: `${p.provider}:${p.provider_place_id}`,
    name: p.name,
    address: p.address ?? null,
    latitude: p.latitude,
    longitude: p.longitude,
  };
}

/**
 * Build the Map "View place" URL. Carries only the PLACE's public identity +
 * snapshot coordinates and the source surface. It deliberately contains NO
 * origin, NO device location, NO user coordinates. `source` defaults to
 * "schedule" so existing callers are unaffected.
 */
export function buildFocusUrl(ref: EventPlaceRef, source: FocusSource = "schedule"): string {
  const q = new URLSearchParams({
    from: source,
    focus_provider: ref.provider,
    focus_id: ref.provider_place_id,
    focus_name: ref.name,
    focus_lat: String(ref.latitude),
    focus_lng: String(ref.longitude),
  });
  if (ref.address) q.set("focus_address", ref.address);
  return `/map?${q.toString()}`;
}

/** The source surface of an inbound Map focus handoff, or null if it isn't one. */
export function parseFocusSource(params: URLSearchParams): FocusSource | null {
  const from = (params.get("from") || "").trim().toLowerCase();
  return from === "schedule" || from === "activity" ? from : null;
}

/**
 * The internal Map "View on map" URL for a real Activity place result, or null
 * when the result lacks the stable identity / valid coordinates needed to focus
 * the Map on it (caller then shows NOTHING rather than a dead link — never a
 * fabricated fallback). Reuses the exact `buildFocusUrl` / `parseFocusRef`
 * contract; the only difference is `from=activity` plus the optional activity id
 * so the Map focus view can send the user back to Activity.
 */
export function activityFocusUrl(
  p: Pick<CorePlace, "provider" | "provider_place_id" | "name" | "latitude" | "longitude"> &
    Partial<Pick<CorePlace, "address">>,
  activityId?: string | null,
): string | null {
  const ref = toPlaceRef(p);
  if (ref.provider !== "google" && ref.provider !== "osm") return null;
  if (ref.provider === "google" && !GOOGLE_ID_RE.test(ref.provider_place_id)) return null;
  if (ref.provider === "osm" && !OSM_ID_RE.test(ref.provider_place_id)) return null;
  if (!ref.name) return null;
  if (!finite(ref.latitude) || ref.latitude < LAT_MIN || ref.latitude > LAT_MAX) return null;
  if (!finite(ref.longitude) || ref.longitude < LNG_MIN || ref.longitude > LNG_MAX) return null;

  let url = buildFocusUrl(ref, "activity");
  const id = (activityId || "").trim();
  if (id) url += `&activity=${encodeURIComponent(id)}`;
  return url;
}

/**
 * Parse + VALIDATE an inbound Map focus handoff. Returns a clean EventPlaceRef or
 * null (never a partially-trusted object). The provider id is shape-checked per
 * provider and the coordinates must be finite and in range.
 */
export function parseFocusRef(params: URLSearchParams): EventPlaceRef | null {
  if (parseFocusSource(params) === null) return null;
  const provider = (params.get("focus_provider") || "").trim().toLowerCase();
  const id = (params.get("focus_id") || "").trim();
  const name = (params.get("focus_name") || "").trim();
  const lat = Number(params.get("focus_lat"));
  const lng = Number(params.get("focus_lng"));
  const address = (params.get("focus_address") || "").trim() || null;

  if (provider !== "google" && provider !== "osm") return null;
  if (provider === "google" && !GOOGLE_ID_RE.test(id)) return null;
  if (provider === "osm" && !OSM_ID_RE.test(id)) return null;
  if (!name) return null;
  if (!finite(lat) || lat < LAT_MIN || lat > LAT_MAX) return null;
  if (!finite(lng) || lng < LNG_MIN || lng > LNG_MAX) return null;

  return {
    provider,
    provider_place_id: id,
    place_id: `${provider}:${id}`,
    name,
    address,
    latitude: lat,
    longitude: lng,
  };
}

/**
 * The structured place actions to render for a schedule event, or `null` when
 * the event has no real linked place (a plain free-text location, or a place
 * that was unlinked). This is the ONE gate the Schedule detail sheet uses so it
 * can never offer "View on map" / "Directions" for structured identity the event
 * no longer holds.
 */
export function linkedPlaceActions(
  place: EventPlaceRef | null | undefined,
): { viewUrl: string; directionsUrl: string } | null {
  if (!place || !place.provider || !place.provider_place_id) return null;
  return { viewUrl: buildFocusUrl(place), directionsUrl: placeDirectionsUrl(place) };
}

/** Body for POST /api/places/resolve — exactly the persisted snapshot fields. */
export function resolveRequestBody(ref: EventPlaceRef): Record<string, unknown> {
  return {
    provider: ref.provider,
    provider_place_id: ref.provider_place_id,
    name: ref.name,
    address: ref.address ?? undefined,
    latitude: ref.latitude,
    longitude: ref.longitude,
  };
}

/**
 * External DIRECTIONS deep link for a linked place. Identity-preserving for
 * Google (`destination_place_id`); a plain coordinate destination otherwise.
 * Never contains an API key and never an `origin` — the external map decides the
 * origin at the user's end, exactly as the Phase 5D Map "Directions" button does.
 */
export function placeDirectionsUrl(
  ref: Pick<EventPlaceRef, "provider" | "provider_place_id" | "name" | "latitude" | "longitude">,
): string {
  const base = "https://www.google.com/maps/dir/?api=1";
  if (ref.provider === "google" && GOOGLE_ID_RE.test(ref.provider_place_id)) {
    return (
      `${base}&destination=${encodeURIComponent(ref.name)}` +
      `&destination_place_id=${encodeURIComponent(ref.provider_place_id)}`
    );
  }
  const dest =
    finite(ref.latitude) && finite(ref.longitude)
      ? `${ref.latitude},${ref.longitude}`
      : encodeURIComponent(ref.name);
  return `${base}&destination=${dest}`;
}

/**
 * Truthful one-line note for a resolve result. `null` when the live lookup
 * succeeded (nothing to explain). Never blames the network unless we actually
 * know the browser is offline (Phase 5G defers connectivity UX to 5H).
 */
export function resolveStatusNote(
  res: Pick<PlaceResolveResponse, "status" | "reason">,
): string | null {
  if (res.status === "LIVE") return null;
  switch (res.reason) {
    case "NO_LIVE_PROVIDER":
      return "Showing the saved place details.";
    case "PROVIDER_NOT_CONFIGURED":
      return "Live place details aren't switched on for this server — showing saved details.";
    case "PROVIDER_NO_MATCH":
      return "The provider no longer lists this place — showing the details saved with your event.";
    case "PROVIDER_TIMEOUT":
    case "PROVIDER_UNAVAILABLE":
    case "PROVIDER_RATE_LIMITED":
    case "INVALID_PROVIDER_RESPONSE":
      return "Couldn't refresh live details right now — showing the details saved with your event.";
    default:
      return "Showing the details saved with your event.";
  }
}
