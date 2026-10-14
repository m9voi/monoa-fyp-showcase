// Launch / First-Run Entry — unit tests for the AuthGuard routing decision.
// Run with Node's built-in test runner (zero deps): `npm run test:node`.
//
// P0 entry-flow cleanup: the web/Safari active path no longer routes an
// unauthenticated visitor through /welcome (that produced an overlapping-
// branding glitch on real iPhone Safari). unauthenticatedDestination() now
// always resolves straight to /login.

import { test } from "node:test";
import assert from "node:assert/strict";

import { unauthenticatedDestination } from "../launchRoute.ts";

test("unauthenticated root ('/') -> /login directly, never /welcome", () => {
  assert.equal(unauthenticatedDestination("/"), "/login");
});

test("unauthenticated deep link to a protected route -> /login?next=<path>", () => {
  assert.equal(unauthenticatedDestination("/finance"), "/login?next=%2Ffinance");
  assert.equal(unauthenticatedDestination("/schedule"), "/login?next=%2Fschedule");
});

test("every route resolves to a /login destination, never /welcome", () => {
  for (const path of ["/", "/finance", "/schedule", "/activity", "/map", "/profile", "/settings"]) {
    assert.ok(unauthenticatedDestination(path).startsWith("/login"), path);
    assert.notEqual(unauthenticatedDestination(path), "/welcome");
  }
});

test("query strings and hashes on the path are preserved in the next param", () => {
  assert.equal(
    unauthenticatedDestination("/map?focus=abc"),
    `/login?next=${encodeURIComponent("/map?focus=abc")}`,
  );
});

test("null/undefined pathname treated as / (matches existing AuthGuard fallback)", () => {
  assert.equal(unauthenticatedDestination(null), "/login");
  assert.equal(unauthenticatedDestination(undefined), "/login");
});
