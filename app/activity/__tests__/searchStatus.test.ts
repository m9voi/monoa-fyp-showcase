// B9 — Activity adaptive nearby search: the "expanded search" note helper.
import { test } from "node:test";
import assert from "node:assert/strict";

import { expandedRadiusNote, BASE_RADIUS_M } from "../searchStatus.ts";

test("no note at or below the 8 km base radius", () => {
  assert.equal(expandedRadiusNote(BASE_RADIUS_M), null);
  assert.equal(expandedRadiusNote(8000), null);
  assert.equal(expandedRadiusNote(5000), null);
});

test("a factual one-line note once the search widened", () => {
  assert.equal(expandedRadiusNote(12000), "Expanded search to 12 km");
  assert.equal(expandedRadiusNote(20000), "Expanded search to 20 km");
});

test("missing / invalid values show nothing rather than a wrong claim", () => {
  for (const v of [undefined, null, NaN, Infinity, "12000", {}] as unknown[]) {
    assert.equal(expandedRadiusNote(v), null);
  }
});
