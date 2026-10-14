// Phase 5D — pure map-state helper tests (Node's built-in runner, zero deps).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  reconcileSelection,
  resultBounds,
  directionsUrl,
  formatDistance,
  distanceLabel,
} from "../mapState.ts";

function place(over = {}) {
  return {
    place_id: "google:ChIJ_a",
    provider: "google",
    provider_place_id: "ChIJ_a",
    name: "Test Court",
    latitude: 3.16,
    longitude: 101.71,
    address: null,
    distance_m: 1200,
    distance_kind: "straight_line",
    activity: "futsal",
    activity_match: "exact",
    provider_types: [],
    rating: null,
    rating_count: null,
    opening_hours: null,
    open_now: null,
    business_status: null,
    website: null,
    phone: null,
    maps_url: "https://maps.google/?cid=1",
    evidence: {
      identity: { source: "google", verified: true },
      operational: { source: null, status: null },
      hours: { source: null, has_hours: false, open_now: null },
      quality: { source: null, rating: null, rating_count: null },
      contact: { source: null, website: null, phone: null },
    },
    ...over,
  };
}

// ---- reconcileSelection ------------------------------------------------
test("reconcileSelection: null stays null", () => {
  assert.equal(reconcileSelection(null, [place()]), null);
});

test("reconcileSelection: keeps a still-present selection", () => {
  const a = place({ place_id: "id-a" });
  const b = place({ place_id: "id-b" });
  assert.equal(reconcileSelection("id-a", [a, b]), "id-a");
});

test("reconcileSelection: clears a selection missing from the new results", () => {
  const b = place({ place_id: "id-b" });
  assert.equal(reconcileSelection("id-a", [b]), null);
  assert.equal(reconcileSelection("id-a", []), null);
});

// ---- resultBounds ----------------------------------------------------
test("resultBounds: null when nothing to frame", () => {
  assert.equal(resultBounds(null, []), null);
});

test("resultBounds: covers user + places", () => {
  const b = resultBounds(
    { latitude: 3.10, longitude: 101.60 },
    [place({ place_id: "1", latitude: 3.20, longitude: 101.70 }),
     place({ place_id: "2", latitude: 3.15, longitude: 101.55 })],
  );
  assert.deepEqual(b, [[3.10, 101.55], [3.20, 101.70]]);
});

test("resultBounds: ignores non-finite coordinates", () => {
  const b = resultBounds(
    { latitude: NaN, longitude: NaN },
    [place({ place_id: "1", latitude: 3.2, longitude: 101.7 }),
     place({ place_id: "2", latitude: Infinity, longitude: 101.7 })],
  );
  assert.deepEqual(b, [[3.2, 101.7], [3.2, 101.7]]);
});

test("resultBounds: user only", () => {
  assert.deepEqual(resultBounds({ latitude: 3.1, longitude: 101.6 }, []), [[3.1, 101.6], [3.1, 101.6]]);
});

// ---- directionsUrl -------------------------------------------------
test("directionsUrl: uses the provider maps_url when https", () => {
  assert.equal(directionsUrl(place({ maps_url: "https://maps.google/?cid=99" })), "https://maps.google/?cid=99");
});

test("directionsUrl: falls back to a coordinate query when maps_url missing", () => {
  const u = directionsUrl(place({ maps_url: "", latitude: 3.161, longitude: 101.712 }));
  assert.equal(u, "https://www.google.com/maps/search/?api=1&query=3.161,101.712");
});

test("directionsUrl: falls back to name when coords are not finite", () => {
  const u = directionsUrl(place({ maps_url: "", latitude: NaN, longitude: NaN, name: "Champion Futsal KL" }));
  assert.equal(u, "https://www.google.com/maps/search/?api=1&query=Champion%20Futsal%20KL");
});

test("directionsUrl: never contains an API key", () => {
  for (const mu of ["https://maps.google/?cid=1", "", "http://insecure"]) {
    const u = directionsUrl(place({ maps_url: mu }));
    assert.ok(!/AIza|api_key|x-goog/i.test(u), u);
  }
});

// Phase 5H-B6 — the provider maps_url goes through the shared external-URL
// allow-list (safeExternalHref). A non-http(s) or host-less value is rejected
// and the safe coordinate/name search link is used instead.
test("directionsUrl: a javascript: maps_url is rejected and falls back", () => {
  const u = directionsUrl(
    place({ maps_url: "javascript:alert(1)", latitude: 3.1, longitude: 101.7 }),
  );
  assert.equal(u, "https://www.google.com/maps/search/?api=1&query=3.1,101.7");
});

test("directionsUrl: a data: / host-less maps_url falls back", () => {
  for (const bad of ["data:text/html,x", "https://", "not a url", "//evil.example"]) {
    const u = directionsUrl(place({ maps_url: bad, latitude: 1.5, longitude: 2.5 }));
    assert.equal(u, "https://www.google.com/maps/search/?api=1&query=1.5,2.5");
  }
});

// ---- formatDistance ----------------------------------------------
test("formatDistance", () => {
  assert.equal(formatDistance(58), "60 m away");
  assert.equal(formatDistance(940), "940 m away");   // < 950 -> metres
  assert.equal(formatDistance(950), "0.9 km away");  // >= 950 -> km
  assert.equal(formatDistance(1000), "1.0 km away");
  assert.equal(formatDistance(2607), "2.6 km away");
  assert.equal(formatDistance(-1), "");
  assert.equal(formatDistance(NaN), "");
});

test("distanceLabel: withholds the metre figure under an unreliable fix", () => {
  assert.equal(distanceLabel(2607, true), "2.6 km away");
  assert.equal(distanceLabel(640, true), "640 m away");
  assert.equal(distanceLabel(640, false), "distance not reliable");
  assert.equal(distanceLabel(2607, false), "distance not reliable");
});
