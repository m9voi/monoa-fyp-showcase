// Phase 5D — device-location sub-state invariants. Run via `npm run test:node`.
// These lock in the fix for the "Getting your location…" hang: a Strict-Mode
// effect replay must not leave the mounted flag stuck false, and the page-level
// fail-safe must always be able to leave "requesting".

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  mountedEffect,
  acquireFailsafe,
  LOCATION_ACQUIRE_DEADLINE_MS,
  type DeviceLoc,
} from "../deviceLoc.ts";
import { LOCATION_WATCHDOG_MS } from "../../lib/location.ts";

// A minimal stand-in for React's effect runner.
function runEffect(body: () => () => void) {
  const cleanup = body();
  return cleanup;
}

// ---- mountedEffect: survives the Strict-Mode setup → cleanup → setup replay ----
test("mountedEffect: ref ends up TRUE after a dev/Strict-Mode replay", () => {
  const ref = { current: true };
  // React (dev) mounts, then replays: setup, cleanup, setup.
  const c1 = runEffect(() => mountedEffect(ref));
  assert.equal(ref.current, true, "after first setup");
  c1();
  assert.equal(ref.current, false, "after the replay cleanup");
  runEffect(() => mountedEffect(ref)); // second setup
  assert.equal(ref.current, true, "second setup MUST re-establish it");
});

test("mountedEffect: a real unmount still sets it false", () => {
  const ref = { current: true };
  const cleanup = runEffect(() => mountedEffect(ref));
  cleanup();
  assert.equal(ref.current, false);
});

test("the OLD cleanup-only pattern is the bug: it ends FALSE after replay", () => {
  // Demonstrates exactly what was wrong before the fix.
  const ref = { current: true };
  const buggy = () => () => { ref.current = false; };
  const c1 = runEffect(buggy);   // setup: nothing
  c1();                          // replay cleanup: false
  runEffect(buggy);             // replay setup: STILL nothing
  assert.equal(ref.current, false, "stuck false while 'mounted' — this hung the UI");
});

// ---- acquireFailsafe: "requesting" can never persist ----
test("acquireFailsafe: requesting -> failed/TIMEOUT with the given message", () => {
  const next = acquireFailsafe({ phase: "requesting" }, "We couldn't get your location in time.");
  assert.deepEqual(next, {
    phase: "failed",
    status: "TIMEOUT",
    message: "We couldn't get your location in time.",
  });
});

test("acquireFailsafe: is idempotent / a no-op for any non-requesting state", () => {
  const idle: DeviceLoc = { phase: "idle" };
  const failed: DeviceLoc = { phase: "failed", status: "PERMISSION_DENIED", message: "x" };
  assert.equal(acquireFailsafe(idle, "t"), idle);
  assert.equal(acquireFailsafe(failed, "t"), failed);
});

test("acquireFailsafe: never fabricates coordinates or a fallback location", () => {
  const next = acquireFailsafe({ phase: "requesting" }, "t");
  const blob = JSON.stringify(next);
  assert.ok(!/lat|lng|latitude|longitude|2\.9213|Cyberjaya/i.test(blob));
});

// ---- the deadline is a real backstop, above the helper's own watchdog ----
test("LOCATION_ACQUIRE_DEADLINE_MS sits above the helper watchdog", () => {
  assert.equal(typeof LOCATION_ACQUIRE_DEADLINE_MS, "number");
  assert.ok(LOCATION_ACQUIRE_DEADLINE_MS > LOCATION_WATCHDOG_MS);
  assert.ok(LOCATION_ACQUIRE_DEADLINE_MS <= 45_000);
});
