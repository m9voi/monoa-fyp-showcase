// Batch 01 — Personal Details pure-helper tests (Node's built-in test runner).
// Run with `npm run test:node`.

import { test } from "node:test";
import assert from "node:assert/strict";

import { NAME_MAX_LENGTH, validateName, isDirty, initials } from "../personalDetails.ts";

test("validateName: rejects an empty name", () => {
  assert.equal(validateName(""), "Enter a name.");
});

test("validateName: rejects a whitespace-only name", () => {
  assert.equal(validateName("   "), "Enter a name.");
});

test("validateName: accepts a normal trimmed name", () => {
  assert.equal(validateName("  Alice Wonderland  "), null);
});

test("validateName: accepts a name at exactly the max length", () => {
  assert.equal(validateName("A".repeat(NAME_MAX_LENGTH)), null);
});

test("validateName: rejects a name over the max length (never silently truncates)", () => {
  const msg = validateName("A".repeat(NAME_MAX_LENGTH + 1));
  assert.ok(msg && msg.includes(String(NAME_MAX_LENGTH)));
});

test("validateName: preserves unicode, does not reject it", () => {
  assert.equal(validateName("Müsab Ünïcödé 名前"), null);
});

test("isDirty: false when the field matches the saved name exactly", () => {
  assert.equal(isDirty("Alice", "Alice"), false);
});

test("isDirty: false when only surrounding whitespace differs", () => {
  assert.equal(isDirty("  Alice  ", "Alice"), false);
});

test("isDirty: true when the field genuinely differs from the saved name", () => {
  assert.equal(isDirty("Alicia", "Alice"), true);
});

test("isDirty: true against a null/undefined saved name (no prior name yet)", () => {
  assert.equal(isDirty("Alice", null), true);
  assert.equal(isDirty("Alice", undefined), true);
});

test("isDirty: false for an empty field against an empty/absent saved name", () => {
  assert.equal(isDirty("", null), false);
  assert.equal(isDirty("   ", undefined), false);
});

test("initials: first + last initial for a two-part name (Figma 261:130's 'MA')", () => {
  assert.equal(initials("Mosab Alsadiq", "mosab@example.com"), "MA");
});

test("initials: a middle name doesn't change the first+last rule", () => {
  assert.equal(initials("Mosab Ahmed Alsadiq", null), "MA");
});

test("initials: single-word name falls back to one letter", () => {
  assert.equal(initials("Mosab", "mosab@example.com"), "M");
});

test("initials: falls back to the email's first letter when there is no name", () => {
  assert.equal(initials(null, "mosab@example.com"), "M");
  assert.equal(initials("", "mosab@example.com"), "M");
});

test("initials: '?' when there is neither a real name nor an email", () => {
  assert.equal(initials(null, null), "?");
  assert.equal(initials("   ", ""), "?");
});
