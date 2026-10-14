// B10 — "Search this area" pure helpers. Run via `npm run test:node`.
// Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  haversineMeters,
  shouldOfferAreaSearch,
  expandedRadiusNote,
  AREA_SEARCH_MIN_MOVE_M,
  ADAPTIVE_BASE_RADIUS_M,
} from "../searchThisArea.ts";

const KLCC = { latitude: 3.1578, longitude: 101.7117 };

test("haversineMeters: same point is 0; a known short hop is sane", () => {
  assert.equal(haversineMeters(KLCC, KLCC), 0);
  // ~1.1 km due north (0.01 deg latitude)
  const d = haversineMeters(KLCC, { latitude: 3.1678, longitude: 101.7117 });
  assert.ok(d > 1050 && d < 1170, `got ${d}`);
});

test("shouldOfferAreaSearch: false before a first search / when centre unknown", () => {
  assert.equal(shouldOfferAreaSearch(null, KLCC), false);
  assert.equal(shouldOfferAreaSearch(KLCC, null), false);
  assert.equal(shouldOfferAreaSearch(null, null), false);
  assert.equal(shouldOfferAreaSearch(undefined, undefined), false);
});

test("shouldOfferAreaSearch: false for sub-threshold jitter, true past the threshold", () => {
  // ~120 m nudge -> jitter, no control
  const jitter = { latitude: KLCC.latitude + 0.0011, longitude: KLCC.longitude };
  assert.ok(haversineMeters(KLCC, jitter) < AREA_SEARCH_MIN_MOVE_M);
  assert.equal(shouldOfferAreaSearch(KLCC, jitter), false);

  // ~2.2 km pan -> intentional move, control appears
  const moved = { latitude: KLCC.latitude + 0.02, longitude: KLCC.longitude };
  assert.ok(haversineMeters(KLCC, moved) > AREA_SEARCH_MIN_MOVE_M);
  assert.equal(shouldOfferAreaSearch(KLCC, moved), true);
});

test("shouldOfferAreaSearch: exactly at the threshold is NOT enough (strict >)", () => {
  // fabricate a pair whose distance is ~ the threshold, then just under/over
  const justUnder = { latitude: KLCC.latitude, longitude: KLCC.longitude };
  assert.equal(shouldOfferAreaSearch(KLCC, justUnder, 0), false); // 0 distance, minMove 0 -> not > 0
  assert.equal(shouldOfferAreaSearch(KLCC, { latitude: KLCC.latitude + 0.02, longitude: KLCC.longitude }, 0), true);
});

test("shouldOfferAreaSearch: non-finite coordinates are rejected", () => {
  assert.equal(shouldOfferAreaSearch({ latitude: Number.NaN, longitude: 101 }, KLCC), false);
  assert.equal(shouldOfferAreaSearch(KLCC, { latitude: 3, longitude: Number.POSITIVE_INFINITY }), false);
});

test("expandedRadiusNote: null at/below base, factual note above", () => {
  assert.equal(expandedRadiusNote(ADAPTIVE_BASE_RADIUS_M), null);
  assert.equal(expandedRadiusNote(5000), null);
  assert.equal(expandedRadiusNote(8000), null);
  assert.equal(expandedRadiusNote(12000), "Expanded search to 12 km");
  assert.equal(expandedRadiusNote(20000), "Expanded search to 20 km");
});

test("expandedRadiusNote: missing / invalid values -> null (never a fake note)", () => {
  for (const bad of [undefined, null, Number.NaN, Number.POSITIVE_INFINITY, "12000", {}, []]) {
    assert.equal(expandedRadiusNote(bad as unknown), null);
  }
});
