import { test } from "node:test";
import assert from "node:assert/strict";

import { toggleValue, isDirty } from "../interestSelection.ts";

test("toggleValue adds a value that is absent", () => {
  assert.deepEqual(toggleValue(["Action"], "RPG"), ["Action", "RPG"]);
});

test("toggleValue removes a value that is present", () => {
  assert.deepEqual(toggleValue(["Action", "RPG"], "Action"), ["RPG"]);
});

test("toggleValue on an empty list adds the value", () => {
  assert.deepEqual(toggleValue([], "Action"), ["Action"]);
});

test("toggling the same value twice returns to the original list", () => {
  const start = ["Action", "RPG"];
  const once = toggleValue(start, "Strategy");
  const twice = toggleValue(once, "Strategy");
  assert.deepEqual(twice, start);
});

test("isDirty is false for identical sets regardless of order", () => {
  assert.equal(isDirty(["Action", "RPG"], ["RPG", "Action"]), false);
});

test("isDirty is false for two empty lists", () => {
  assert.equal(isDirty([], []), false);
});

test("isDirty is true when lengths differ", () => {
  assert.equal(isDirty(["Action"], []), true);
  assert.equal(isDirty([], ["Action"]), true);
});

test("isDirty is true when same length but different content", () => {
  assert.equal(isDirty(["Action"], ["RPG"]), true);
});

test("isDirty is true for a genuinely different selection with overlap", () => {
  assert.equal(isDirty(["Action", "RPG"], ["Action", "Strategy"]), true);
});
