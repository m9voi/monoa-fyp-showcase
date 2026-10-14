import test from "node:test";
import assert from "node:assert/strict";
import { NAV_ITEMS, isNavActive } from "../nav.ts";

test("NAV_ITEMS is the canonical five, in order", () => {
  assert.deepEqual(
    NAV_ITEMS.map((i) => i.key),
    ["today", "plan", "mo", "activity", "wallet"],
  );
  assert.deepEqual(
    NAV_ITEMS.map((i) => i.label),
    ["Today", "Plan", "Mo", "Activity", "Wallet"],
  );
  assert.deepEqual(
    NAV_ITEMS.map((i) => i.href),
    ["/", "/schedule", "/mo", "/activity", "/finance"],
  );
});

test("Mo is the centre destination and points at a real /mo route", () => {
  assert.equal(NAV_ITEMS[2].key, "mo");
  assert.equal(NAV_ITEMS[2].center, true);
  assert.equal(NAV_ITEMS[2].href, "/mo");
});

test("no primary destination points at /map", () => {
  assert.ok(!NAV_ITEMS.some((i) => i.href === "/map"));
});

test("isNavActive: Today matches only the exact root", () => {
  assert.equal(isNavActive("/", "/"), true);
  assert.equal(isNavActive("/schedule", "/"), false);
  assert.equal(isNavActive("/finance/history", "/"), false);
  assert.equal(isNavActive("/mo", "/"), false);
});

test("isNavActive: a section matches itself and its descendants", () => {
  assert.equal(isNavActive("/schedule", "/schedule"), true);
  assert.equal(isNavActive("/mo", "/mo"), true);
  assert.equal(isNavActive("/activity", "/activity"), true);
  assert.equal(isNavActive("/finance", "/finance"), true);
  assert.equal(isNavActive("/finance/history", "/finance"), true);
});

test("isNavActive: unrelated routes are not active", () => {
  assert.equal(isNavActive("/map", "/finance"), false);
  assert.equal(isNavActive("/map", "/activity"), false);
  assert.equal(isNavActive("/settings", "/schedule"), false);
  assert.equal(isNavActive("/profile", "/"), false);
});
