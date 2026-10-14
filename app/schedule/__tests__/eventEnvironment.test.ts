// B5 — event environment classification (Node's built-in runner).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  environmentForActivity,
  ENVIRONMENT_LABEL,
} from "../eventEnvironment.ts";

test("default is uncertain, never outdoor, for an unknown activity and no place types", () => {
  assert.equal(environmentForActivity(null, null), "uncertain");
  assert.equal(environmentForActivity("kite-surfing", []), "uncertain");
  assert.equal(environmentForActivity("badminton", []), "uncertain");
  assert.equal(environmentForActivity("futsal", undefined), "uncertain");
});

test("open-geography sports are outdoor by nature", () => {
  assert.equal(environmentForActivity("running", []), "outdoor");
  assert.equal(environmentForActivity("cycling", []), "outdoor");
  assert.equal(environmentForActivity("golf", []), "outdoor");
});

test("an indoor place type keeps a hall sport indoor", () => {
  assert.equal(environmentForActivity("badminton", ["sport=badminton", "leisure=sports_hall"]), "indoor");
  assert.equal(environmentForActivity("futsal", ["gym", "fitness_center"]), "indoor");
});

test("an indoor place type overrides an otherwise-outdoor activity (indoor track)", () => {
  assert.equal(environmentForActivity("running", ["sports_hall", "indoor=yes"]), "indoor");
});

test("an outdoor place type promotes an uncertain hall sport to outdoor", () => {
  assert.equal(environmentForActivity("badminton", ["leisure=pitch", "park"]), "outdoor");
  assert.equal(environmentForActivity("tennis", ["leisure=park"]), "outdoor");
});

test("outdoor activity + outdoor place stays outdoor", () => {
  assert.equal(environmentForActivity("running", ["leisure=track", "sport=athletics"]), "outdoor");
});

test("conflicting signals fall back to the reviewed base (never invent outdoor)", () => {
  assert.equal(environmentForActivity("badminton", ["park", "gym"]), "uncertain");
  assert.equal(environmentForActivity("running", ["park", "sports_hall"]), "outdoor");
});

test("classification is case-insensitive on the activity id", () => {
  assert.equal(environmentForActivity("Running", []), "outdoor");
  assert.equal(environmentForActivity("  BADMINTON ", []), "uncertain");
});

test("labels exist for all three", () => {
  assert.deepEqual(Object.keys(ENVIRONMENT_LABEL).sort(), ["indoor", "outdoor", "uncertain"]);
});
