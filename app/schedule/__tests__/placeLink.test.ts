// Phase 5G — Schedule <-> Map bridge helper tests (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  toPlaceRef,
  buildFocusUrl,
  parseFocusRef,
  parseFocusSource,
  activityFocusUrl,
  resolveRequestBody,
  placeDirectionsUrl,
  resolveStatusNote,
  linkedPlaceActions,
} from "../placeLink.ts";

const GOOGLE_REF = {
  provider: "google",
  provider_place_id: "ChIJ_klcc_park_01",
  place_id: "google:ChIJ_klcc_park_01",
  name: "KLCC Park",
  address: "Kuala Lumpur City Centre",
  latitude: 3.1553,
  longitude: 101.7137,
};

test("toPlaceRef narrows a CorePlace-shaped object to stable identity", () => {
  const ref = toPlaceRef({
    provider: "google",
    provider_place_id: "ChIJ_x",
    name: "Somewhere",
    latitude: 1.5,
    longitude: 103.2,
    address: "Addr",
  });
  assert.deepEqual(ref, {
    provider: "google",
    provider_place_id: "ChIJ_x",
    place_id: "google:ChIJ_x",
    name: "Somewhere",
    address: "Addr",
    latitude: 1.5,
    longitude: 103.2,
  });
});

test("buildFocusUrl carries place identity + snapshot, and NO origin", () => {
  const url = buildFocusUrl(GOOGLE_REF);
  const q = new URLSearchParams(url.split("?")[1]);
  assert.equal(q.get("from"), "schedule");
  assert.equal(q.get("focus_provider"), "google");
  assert.equal(q.get("focus_id"), "ChIJ_klcc_park_01");
  assert.equal(q.get("focus_name"), "KLCC Park");
  assert.equal(Number(q.get("focus_lat")), 3.1553);
  assert.equal(Number(q.get("focus_lng")), 101.7137);
  // hard rule: the user's origin / device location must never appear
  assert.ok(!/origin|device|user_lat|user_lng|my_lat/i.test(url));
});

test("buildFocusUrl -> parseFocusRef round-trips", () => {
  const url = buildFocusUrl(GOOGLE_REF);
  const parsed = parseFocusRef(new URLSearchParams(url.split("?")[1]));
  assert.deepEqual(parsed, GOOGLE_REF);
});

test("parseFocusRef rejects a foreign / malformed handoff", () => {
  // not from schedule
  assert.equal(parseFocusRef(new URLSearchParams("focus_provider=google&focus_id=ChIJ_x&focus_name=A&focus_lat=1&focus_lng=2")), null);
  // bad provider
  assert.equal(parseFocusRef(new URLSearchParams("from=schedule&focus_provider=yelp&focus_id=abcd&focus_name=A&focus_lat=1&focus_lng=2")), null);
  // bad google id
  assert.equal(parseFocusRef(new URLSearchParams("from=schedule&focus_provider=google&focus_id=%21%21&focus_name=A&focus_lat=1&focus_lng=2")), null);
  // out-of-range coords
  assert.equal(parseFocusRef(new URLSearchParams("from=schedule&focus_provider=google&focus_id=abcd&focus_name=A&focus_lat=999&focus_lng=2")), null);
  // non-finite
  assert.equal(parseFocusRef(new URLSearchParams("from=schedule&focus_provider=google&focus_id=abcd&focus_name=A&focus_lat=x&focus_lng=2")), null);
  // missing name
  assert.equal(parseFocusRef(new URLSearchParams("from=schedule&focus_provider=google&focus_id=abcd&focus_name=&focus_lat=1&focus_lng=2")), null);
});

test("parseFocusRef accepts a valid OSM handoff", () => {
  const ref = parseFocusRef(new URLSearchParams(
    "from=schedule&focus_provider=osm&focus_id=way%3A987654&focus_name=Taman&focus_lat=3.14&focus_lng=101.68",
  ));
  assert.equal(ref?.provider, "osm");
  assert.equal(ref?.provider_place_id, "way:987654");
  assert.equal(ref?.place_id, "osm:way:987654");
});

test("resolveRequestBody is exactly the persisted snapshot fields", () => {
  assert.deepEqual(resolveRequestBody(GOOGLE_REF), {
    provider: "google",
    provider_place_id: "ChIJ_klcc_park_01",
    name: "KLCC Park",
    address: "Kuala Lumpur City Centre",
    latitude: 3.1553,
    longitude: 101.7137,
  });
});

test("placeDirectionsUrl preserves Google place identity, never a key or origin", () => {
  const url = placeDirectionsUrl(GOOGLE_REF);
  assert.ok(url.startsWith("https://www.google.com/maps/dir/?api=1"));
  assert.ok(url.includes("destination_place_id=ChIJ_klcc_park_01"));
  assert.ok(!/[?&]origin=/.test(url));
  assert.ok(!/key=/i.test(url));
});

test("placeDirectionsUrl falls back to a coordinate destination for OSM", () => {
  const url = placeDirectionsUrl({
    provider: "osm",
    provider_place_id: "way:1",
    name: "Taman",
    latitude: 3.14,
    longitude: 101.68,
  });
  assert.ok(url.includes("destination=3.14,101.68"));
  assert.ok(!url.includes("destination_place_id"));
});

test("linkedPlaceActions: null when the event has no structured place (post-unlink)", () => {
  // after a manual free-text location edit / explicit unlink, occ.place is null
  assert.equal(linkedPlaceActions(null), null);
  assert.equal(linkedPlaceActions(undefined), null);
  // a malformed / half-cleared ref must not produce structured actions either
  assert.equal(linkedPlaceActions({ ...GOOGLE_REF, provider: "", provider_place_id: "" }), null);
});

test("linkedPlaceActions: identity-preserving actions when a real place is linked", () => {
  const a = linkedPlaceActions(GOOGLE_REF);
  assert.ok(a);
  assert.ok(a!.viewUrl.includes("focus_id=ChIJ_klcc_park_01"));
  assert.ok(a!.directionsUrl.includes("destination_place_id=ChIJ_klcc_park_01"));
});

// --- B1.1: contextual Activity -> internal Map handoff (same URL contract) ---

test("buildFocusUrl defaults to from=schedule; source overrides it", () => {
  assert.equal(new URLSearchParams(buildFocusUrl(GOOGLE_REF).split("?")[1]).get("from"), "schedule");
  assert.equal(
    new URLSearchParams(buildFocusUrl(GOOGLE_REF, "activity").split("?")[1]).get("from"),
    "activity",
  );
  // still no user origin / device coordinates in the activity variant
  assert.ok(!/origin|device|user_lat|user_lng|my_lat/i.test(buildFocusUrl(GOOGLE_REF, "activity")));
});

test("parseFocusSource recognises schedule and activity, rejects anything else", () => {
  assert.equal(parseFocusSource(new URLSearchParams("from=schedule")), "schedule");
  assert.equal(parseFocusSource(new URLSearchParams("from=activity")), "activity");
  assert.equal(parseFocusSource(new URLSearchParams("from=yelp")), null);
  assert.equal(parseFocusSource(new URLSearchParams("")), null);
});

test("an activity focus URL round-trips through parseFocusRef to the same place", () => {
  const url = buildFocusUrl(GOOGLE_REF, "activity");
  const parsed = parseFocusRef(new URLSearchParams(url.split("?")[1]));
  assert.deepEqual(parsed, GOOGLE_REF);
});

test("activityFocusUrl builds an internal /map link from a real result + activity id", () => {
  const url = activityFocusUrl(
    { provider: "osm", provider_place_id: "way:987654", name: "Community Court", latitude: 3.14, longitude: 101.68, address: "Jln X" },
    "futsal",
  );
  assert.ok(url);
  const q = new URLSearchParams(url!.split("?")[1]);
  assert.ok(url!.startsWith("/map?"));
  assert.equal(q.get("from"), "activity");
  assert.equal(q.get("focus_provider"), "osm");
  assert.equal(q.get("focus_id"), "way:987654");
  assert.equal(q.get("activity"), "futsal");
  assert.equal(Number(q.get("focus_lat")), 3.14);
  // never carries the user's own location
  assert.ok(!/origin|device|user_lat|user_lng/i.test(url!));
});

test("activityFocusUrl returns null when the result lacks stable identity / valid coords", () => {
  // type-valid shapes, RUNTIME-invalid values — the helper must reject each.
  const bad = [
    { provider: "yelp", provider_place_id: "abcd", name: "X", latitude: 1, longitude: 2 },
    { provider: "google", provider_place_id: "!!", name: "X", latitude: 1, longitude: 2 },
    { provider: "osm", provider_place_id: "way:1", name: "", latitude: 1, longitude: 2 },
    { provider: "osm", provider_place_id: "way:1", name: "X", latitude: 999, longitude: 2 },
    { provider: "osm", provider_place_id: "way:1", name: "X", latitude: Number.NaN, longitude: 2 },
  ];
  for (const p of bad) {
    assert.equal(activityFocusUrl(p, "futsal"), null);
  }
});

test("activityFocusUrl omits the activity segment when no id is given", () => {
  const url = activityFocusUrl(GOOGLE_REF);
  assert.ok(url && !url.includes("&activity="));
});

// B9.1 regression: the Activity result -> internal Map click-through. The URL the
// card links to must parse back, on the Map side, to the SAME place identity and
// carry from=activity, so the focus view opens on that place and offers
// "Back to activity". (A real OSM `way:` result, as returned by the engine.)
test("activityFocusUrl output round-trips through the Map focus parser to the same place", () => {
  const result = {
    provider: "osm",
    provider_place_id: "way:100201",
    name: "Royal Selangor Golf Club",
    latitude: 3.146,
    longitude: 101.727,
    address: "Kuala Lumpur",
  };
  const url = activityFocusUrl(result, "golf");
  assert.ok(url && url.startsWith("/map?"));
  const params = new URLSearchParams(url!.split("?")[1]);
  assert.equal(parseFocusSource(params), "activity");
  assert.deepEqual(parseFocusRef(params), {
    provider: "osm",
    provider_place_id: "way:100201",
    place_id: "osm:way:100201",
    name: "Royal Selangor Golf Club",
    address: "Kuala Lumpur",
    latitude: 3.146,
    longitude: 101.727,
  });
  assert.equal(params.get("activity"), "golf");
});

test("resolveStatusNote is null for LIVE and truthful for every SNAPSHOT reason", () => {
  assert.equal(resolveStatusNote({ status: "LIVE", reason: null }), null);
  for (const reason of [
    "NO_LIVE_PROVIDER",
    "PROVIDER_NOT_CONFIGURED",
    "PROVIDER_NO_MATCH",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_RATE_LIMITED",
    "INVALID_PROVIDER_RESPONSE",
  ] as const) {
    const note = resolveStatusNote({ status: "SNAPSHOT", reason });
    assert.ok(note && note.length > 0);
    // never claims the user lost internet (connectivity UX is deferred to 5H)
    assert.ok(!/lost.*internet|you.*offline/i.test(note));
  }
});
