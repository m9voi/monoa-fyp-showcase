// Phase 5D — the explicit search-origin model. Pure, DOM-free.
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  originLabel,
  originDistanceReliable,
  originIsVeryApproximate,
  type SearchOrigin,
} from "../searchOrigin.ts";

const device = (accuracyMeters: number | null): SearchOrigin => ({
  source: "device",
  latitude: 2.92,
  longitude: 101.65,
  accuracyMeters,
});
const manual = (label: string): SearchOrigin => ({
  source: "manual",
  latitude: 3.158,
  longitude: 101.712,
  label,
});

// ---- originLabel ------------------------------------------------------
test("originLabel: device -> 'Your location'", () => {
  assert.equal(originLabel(device(40)), "Your location");
  assert.equal(originLabel(device(null)), "Your location");
});

test("originLabel: manual -> the chosen label verbatim", () => {
  assert.equal(originLabel(manual("Cyberjaya, Selangor")), "Cyberjaya, Selangor");
});

// ---- originDistanceReliable ----------------------------------------
test("originDistanceReliable: a normal device fix is reliable", () => {
  assert.equal(originDistanceReliable(device(null)), true);
  assert.equal(originDistanceReliable(device(50)), true);
  assert.equal(originDistanceReliable(device(5_000)), true);
  assert.equal(originDistanceReliable(device(20_000)), true); // boundary: not > threshold
});

test("originDistanceReliable: a very coarse device fix is NOT reliable", () => {
  assert.equal(originDistanceReliable(device(20_001)), false);
  assert.equal(originDistanceReliable(device(75_000)), false);
});

test("originDistanceReliable: a manual origin is always reliable", () => {
  assert.equal(originDistanceReliable(manual("KLCC")), true);
});

// ---- originIsVeryApproximate --------------------------------------
test("originIsVeryApproximate: only a very coarse device fix qualifies", () => {
  assert.equal(originIsVeryApproximate(null), false);
  assert.equal(originIsVeryApproximate(device(40)), false);
  assert.equal(originIsVeryApproximate(device(20_000)), false);
  assert.equal(originIsVeryApproximate(device(20_001)), true);
  assert.equal(originIsVeryApproximate(manual("Putrajaya")), false);
});
