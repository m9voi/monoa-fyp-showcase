// Phase 5B — location foundation unit tests.
// Run with Node's built-in test runner (zero deps): `npm run test:node`.
// Node strips the TS types at load time. This file is excluded from tsconfig
// and eslint (it is not part of the app graph or the Next build).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  validateCoordinates,
  normalizeGeolocationError,
  isRetryable,
  locationRecoveryAction,
  locationErrorMessage,
  makeLocationError,
  isLocationFresh,
  isAccuracyCoarse,
  isAccuracyUnreliable,
  isFiniteNumber,
  isGeolocationSupported,
  isInsecureContext,
  getCurrentLocation,
  LOCATION_TIMEOUT_MS,
  LOCATION_MAX_AGE_MS,
  LOCATION_HIGH_ACCURACY_DEFAULT,
  LOCATION_COARSE_ACCURACY_M,
  LOCATION_WATCHDOG_MS,
} from "../location.ts";

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// --------------------------------------------------------------------------- //
// global mocking helpers (Node has a read-only `navigator` global)
// --------------------------------------------------------------------------- //
function setNavigator(value) {
  Object.defineProperty(globalThis, "navigator", { value, configurable: true, writable: true });
}
function clearNavigator() {
  Object.defineProperty(globalThis, "navigator", { value: undefined, configurable: true, writable: true });
}
function setWindow(value) {
  globalThis.window = value;
}
function clearWindow() {
  delete globalThis.window;
}
function resetEnv() {
  clearWindow();
  clearNavigator();
}

// --------------------------------------------------------------------------- //
// validateCoordinates
// --------------------------------------------------------------------------- //
test("validateCoordinates: accepts a normal fix", () => {
  const r = validateCoordinates(3.139, 101.686, 25, 1_700_000_000_000);
  assert.equal(r.ok, true);
  assert.deepEqual(r.coordinates, {
    latitude: 3.139,
    longitude: 101.686,
    accuracyMeters: 25,
    timestamp: 1_700_000_000_000,
  });
});

test("validateCoordinates: accepts the extremes", () => {
  assert.equal(validateCoordinates(-90, -180, 0).ok, true);
  assert.equal(validateCoordinates(90, 180, 1).ok, true);
});

test("validateCoordinates: rejects out-of-range latitude", () => {
  assert.equal(validateCoordinates(90.001, 10, 5).ok, false);
  assert.equal(validateCoordinates(-91, 10, 5).ok, false);
});

test("validateCoordinates: rejects out-of-range longitude", () => {
  assert.equal(validateCoordinates(10, 180.5, 5).ok, false);
  assert.equal(validateCoordinates(10, -181, 5).ok, false);
});

test("validateCoordinates: rejects NaN", () => {
  assert.equal(validateCoordinates(NaN, 10, 5).ok, false);
  assert.equal(validateCoordinates(10, NaN, 5).ok, false);
});

test("validateCoordinates: rejects Infinity", () => {
  assert.equal(validateCoordinates(Infinity, 10, 5).ok, false);
  assert.equal(validateCoordinates(10, -Infinity, 5).ok, false);
});

test("validateCoordinates: rejects non-number inputs", () => {
  assert.equal(validateCoordinates("3.1", "101", 5).ok, false);
  assert.equal(validateCoordinates(null, undefined, 5).ok, false);
});

test("validateCoordinates: normalises a valid accuracy", () => {
  assert.equal(validateCoordinates(1, 2, 42).coordinates?.accuracyMeters, 42);
});

test("validateCoordinates: bad accuracy -> null, fix still ok", () => {
  for (const bad of [-5, NaN, Infinity, "20", null, undefined]) {
    const r = validateCoordinates(1, 2, bad);
    assert.equal(r.ok, true, `accuracy=${String(bad)}`);
    assert.equal(r.coordinates?.accuracyMeters, null);
  }
});

test("validateCoordinates: missing/invalid timestamp -> defaulted", () => {
  const before = Date.now();
  const r = validateCoordinates(1, 2, 5);
  assert.ok((r.coordinates?.timestamp ?? 0) >= before);
});

// --------------------------------------------------------------------------- //
// normalizeGeolocationError
// --------------------------------------------------------------------------- //
test("normalizeGeolocationError: code 1 -> PERMISSION_DENIED (not retryable)", () => {
  const e = normalizeGeolocationError({ code: 1, message: "denied" });
  assert.equal(e.status, "PERMISSION_DENIED");
  assert.equal(e.retryable, false);
  assert.ok(e.message.length > 0);
  assert.ok(!/code|GeolocationPositionError/i.test(e.message));
});

test("normalizeGeolocationError: code 2 -> POSITION_UNAVAILABLE (retryable)", () => {
  const e = normalizeGeolocationError({ code: 2 });
  assert.equal(e.status, "POSITION_UNAVAILABLE");
  assert.equal(e.retryable, true);
});

test("normalizeGeolocationError: code 3 -> TIMEOUT (retryable)", () => {
  const e = normalizeGeolocationError({ code: 3 });
  assert.equal(e.status, "TIMEOUT");
  assert.equal(e.retryable, true);
});

test("normalizeGeolocationError: accepts a bare numeric code", () => {
  assert.equal(normalizeGeolocationError(1).status, "PERMISSION_DENIED");
  assert.equal(normalizeGeolocationError(3).status, "TIMEOUT");
});

test("normalizeGeolocationError: unknown / malformed -> UNKNOWN_ERROR (retryable)", () => {
  for (const x of [{ code: 99 }, {}, null, undefined, "boom", { code: "1" }]) {
    const e = normalizeGeolocationError(x);
    assert.equal(e.status, "UNKNOWN_ERROR", JSON.stringify(x));
    assert.equal(e.retryable, true);
  }
});

// --------------------------------------------------------------------------- //
// retry policy
// --------------------------------------------------------------------------- //
test("isRetryable: permanent states are not retryable", () => {
  assert.equal(isRetryable("PERMISSION_DENIED"), false);
  assert.equal(isRetryable("INSECURE_CONTEXT"), false);
  assert.equal(isRetryable("UNSUPPORTED"), false);
});

test("isRetryable: transient states are retryable", () => {
  assert.equal(isRetryable("TIMEOUT"), true);
  assert.equal(isRetryable("POSITION_UNAVAILABLE"), true);
  assert.equal(isRetryable("BACKEND_UNREACHABLE"), true);
  assert.equal(isRetryable("UNKNOWN_ERROR"), true);
  assert.equal(isRetryable("INVALID_COORDINATES"), true);
});

// Phase 5J — the failed-location panel's PRIMARY control. A non-retryable
// failure (permission denied on iOS Safari especially) cannot be recovered by a
// same-page retry, so the panel must offer a reload, not a dead "Try again".
test("locationRecoveryAction: non-retryable failures -> reload", () => {
  assert.equal(locationRecoveryAction("PERMISSION_DENIED"), "reload");
  assert.equal(locationRecoveryAction("INSECURE_CONTEXT"), "reload");
  assert.equal(locationRecoveryAction("UNSUPPORTED"), "reload");
});

test("locationRecoveryAction: retryable failures -> retry", () => {
  for (const s of [
    "TIMEOUT",
    "POSITION_UNAVAILABLE",
    "BACKEND_UNREACHABLE",
    "INVALID_COORDINATES",
    "UNKNOWN_ERROR",
  ]) {
    assert.equal(locationRecoveryAction(s), "retry", s);
  }
});

// B11.1 — SERVICES_OFF: the account's Location Services preference is off, so
// MONOA has NOT called navigator.geolocation. It is a distinct, non-retryable
// state whose recovery is "go to Settings" (a same-page retry / reload can't
// change an account preference). Manual "Search a location" stays available.
test("SERVICES_OFF: distinct, non-retryable, recovery = settings", () => {
  assert.equal(isRetryable("SERVICES_OFF"), false);
  assert.equal(locationRecoveryAction("SERVICES_OFF"), "settings");
  const e = makeLocationError("SERVICES_OFF");
  assert.equal(e.status, "SERVICES_OFF");
  assert.equal(e.retryable, false);
  // honest copy: never claims the web app changed a device/OS permission
  assert.ok(/settings/i.test(e.message));
  assert.ok(/search/i.test(e.message)); // points at the manual fallback
  assert.ok(!/we (turned|changed|enabled|disabled)/i.test(e.message));
  // it is NOT conflated with a browser denial
  assert.notEqual(makeLocationError("PERMISSION_DENIED").message, e.message);
});

test("locationErrorMessage covers SERVICES_OFF", () => {
  assert.ok(locationErrorMessage("SERVICES_OFF").length > 0);
});

test("PERMISSION_DENIED copy guides to a reload, not an in-page retry", () => {
  const msg = locationErrorMessage("PERMISSION_DENIED").toLowerCase();
  assert.ok(msg.includes("reload"), msg);
  assert.ok(!msg.includes("try again"), msg);
});

// --------------------------------------------------------------------------- //
// freshness / staleness
// --------------------------------------------------------------------------- //
test("isLocationFresh: recent fix is fresh", () => {
  const now = 1_000_000_000_000;
  assert.equal(isLocationFresh(now - 1_000, now), true);
  assert.equal(isLocationFresh(now - LOCATION_MAX_AGE_MS, now), true);
});

test("isLocationFresh: stale fix is not fresh", () => {
  const now = 1_000_000_000_000;
  assert.equal(isLocationFresh(now - LOCATION_MAX_AGE_MS - 1, now), false);
  assert.equal(isLocationFresh(now - 10 * 60_000, now), false);
});

test("isLocationFresh: invalid / future timestamps are not fresh", () => {
  const now = 1_000_000_000_000;
  assert.equal(isLocationFresh(0, now), false);
  assert.equal(isLocationFresh(-1, now), false);
  assert.equal(isLocationFresh(NaN, now), false);
  assert.equal(isLocationFresh(now + 5_000, now), false);
});

test("isAccuracyUnreliable: only the very coarse (city/IP-level) fixes", () => {
  assert.equal(isAccuracyUnreliable(null), false);
  assert.equal(isAccuracyUnreliable(50), false);
  assert.equal(isAccuracyUnreliable(5_000), false);      // coarse but usable
  assert.equal(isAccuracyUnreliable(15_000), false);     // still under the tier
  assert.equal(isAccuracyUnreliable(20_000), false);     // boundary is exclusive
  assert.equal(isAccuracyUnreliable(20_001), true);
  assert.equal(isAccuracyUnreliable(30_000), true);      // ~the observed KL/Cyberjaya error
  assert.equal(isAccuracyUnreliable(NaN), false);
  // an unreliable fix is ALSO coarse (nested tiers)
  assert.equal(isAccuracyCoarse(30_000), true);
});

test("isAccuracyCoarse: only flags genuinely coarse fixes", () => {
  assert.equal(isAccuracyCoarse(null), false);
  assert.equal(isAccuracyCoarse(50), false);
  assert.equal(isAccuracyCoarse(LOCATION_COARSE_ACCURACY_M), false);
  assert.equal(isAccuracyCoarse(LOCATION_COARSE_ACCURACY_M + 1), true);
});

test("isFiniteNumber", () => {
  assert.equal(isFiniteNumber(0), true);
  assert.equal(isFiniteNumber(-12.5), true);
  assert.equal(isFiniteNumber(NaN), false);
  assert.equal(isFiniteNumber(Infinity), false);
  assert.equal(isFiniteNumber("1"), false);
});

// --------------------------------------------------------------------------- //
// environment detection
// --------------------------------------------------------------------------- //
test("isGeolocationSupported: false when navigator/geolocation absent", (t) => {
  t.after(resetEnv);
  clearNavigator();
  assert.equal(isGeolocationSupported(), false);
  setNavigator({});
  assert.equal(isGeolocationSupported(), false);
  setNavigator({ geolocation: {} });
  assert.equal(isGeolocationSupported(), false);
});

test("isGeolocationSupported: true when the API is present", (t) => {
  t.after(resetEnv);
  setNavigator({ geolocation: { getCurrentPosition() {} } });
  assert.equal(isGeolocationSupported(), true);
});

test("isInsecureContext: only true when positively insecure", (t) => {
  t.after(resetEnv);
  clearWindow();
  assert.equal(isInsecureContext(), false); // unknown -> not assumed insecure
  setWindow({ isSecureContext: true });
  assert.equal(isInsecureContext(), false);
  setWindow({ isSecureContext: false });
  assert.equal(isInsecureContext(), true);
  setWindow({});
  assert.equal(isInsecureContext(), false);
});

// --------------------------------------------------------------------------- //
// getCurrentLocation — always resolves, never rejects
// --------------------------------------------------------------------------- //
test("getCurrentLocation: UNSUPPORTED when geolocation missing", async (t) => {
  t.after(resetEnv);
  setNavigator({});
  const r = await getCurrentLocation();
  assert.equal(r.status, "UNSUPPORTED");
  assert.equal(r.retryable, false);
});

test("getCurrentLocation: INSECURE_CONTEXT when window is positively insecure", async (t) => {
  t.after(resetEnv);
  setNavigator({ geolocation: { getCurrentPosition() {} } });
  setWindow({ isSecureContext: false });
  const r = await getCurrentLocation();
  assert.equal(r.status, "INSECURE_CONTEXT");
  assert.equal(r.retryable, false);
});

test("getCurrentLocation: AVAILABLE on a good fix, with chosen options", async (t) => {
  t.after(resetEnv);
  let seenOpts;
  setNavigator({
    geolocation: {
      getCurrentPosition(ok, _err, options) {
        seenOpts = options;
        ok({ coords: { latitude: 3.1, longitude: 101.7, accuracy: 30 }, timestamp: 1_700_000_000_000 });
      },
    },
  });
  const r = await getCurrentLocation();
  assert.equal(r.status, "AVAILABLE");
  assert.equal(r.coordinates.latitude, 3.1);
  assert.equal(r.coordinates.longitude, 101.7);
  assert.equal(r.coordinates.accuracyMeters, 30);
  assert.equal(seenOpts.enableHighAccuracy, LOCATION_HIGH_ACCURACY_DEFAULT);
  assert.equal(seenOpts.enableHighAccuracy, false);
  assert.equal(seenOpts.timeout, LOCATION_TIMEOUT_MS);
  assert.equal(seenOpts.timeout, 15_000);
  assert.equal(seenOpts.maximumAge, LOCATION_MAX_AGE_MS);
  assert.equal(seenOpts.maximumAge, 120_000);
});

test("getCurrentLocation: INVALID_COORDINATES when the device returns garbage", async (t) => {
  t.after(resetEnv);
  setNavigator({
    geolocation: {
      getCurrentPosition(ok) {
        ok({ coords: { latitude: 999, longitude: 0, accuracy: 5 }, timestamp: Date.now() });
      },
    },
  });
  const r = await getCurrentLocation();
  assert.equal(r.status, "INVALID_COORDINATES");
});

test("getCurrentLocation: PERMISSION_DENIED / TIMEOUT normalised from the error cb", async (t) => {
  t.after(resetEnv);
  for (const [code, expected] of [
    [1, "PERMISSION_DENIED"],
    [2, "POSITION_UNAVAILABLE"],
    [3, "TIMEOUT"],
    [42, "UNKNOWN_ERROR"],
  ]) {
    setNavigator({
      geolocation: {
        getCurrentPosition(_ok, err) {
          err({ code });
        },
      },
    });
    const r = await getCurrentLocation();
    assert.equal(r.status, expected, `code ${code}`);
  }
});

test("getCurrentLocation: does not reject even if getCurrentPosition throws", async (t) => {
  t.after(resetEnv);
  setNavigator({
    geolocation: {
      getCurrentPosition() {
        throw new Error("boom");
      },
    },
  });
  const r = await getCurrentLocation();
  assert.equal(r.status, "UNKNOWN_ERROR");
});

test("getCurrentLocation: honours a caller override", async (t) => {
  t.after(resetEnv);
  let seenOpts;
  setNavigator({
    geolocation: {
      getCurrentPosition(ok, _err, options) {
        seenOpts = options;
        ok({ coords: { latitude: 1, longitude: 2, accuracy: 1 }, timestamp: Date.now() });
      },
    },
  });
  await getCurrentLocation({ timeoutMs: 5_000, highAccuracy: true, maxAgeMs: 0 });
  assert.equal(seenOpts.timeout, 5_000);
  assert.equal(seenOpts.enableHighAccuracy, true);
  assert.equal(seenOpts.maximumAge, 0);
});

// --------------------------------------------------------------------------- //
// CoreOS watchdog — the promise ALWAYS settles, even if the browser never
// invokes either callback (permission prompt ignored, dead OS location
// provider, engine bug). The UI must never hang in "Getting your location…".
// --------------------------------------------------------------------------- //
test("getCurrentLocation: watchdog settles to TIMEOUT when NO callback ever fires", async (t) => {
  t.after(resetEnv);
  let calls = 0;
  setNavigator({
    geolocation: {
      getCurrentPosition() {
        calls++; // never calls ok() or err()
      },
    },
  });
  const started = Date.now();
  const r = await getCurrentLocation({ watchdogMs: 40 });
  assert.equal(calls, 1);
  assert.equal(r.status, "TIMEOUT");
  assert.equal(r.retryable, true);
  assert.ok(!/watchdog|timer|code/i.test(r.message)); // user-facing copy only
  assert.ok(Date.now() - started >= 35);
});

test("getCurrentLocation: a LATE success after the watchdog fired is ignored", async (t) => {
  t.after(resetEnv);
  let ok;
  setNavigator({
    geolocation: {
      getCurrentPosition(success) {
        ok = success; // stash it, fire it late
      },
    },
  });
  const r = await getCurrentLocation({ watchdogMs: 30 });
  assert.equal(r.status, "TIMEOUT");
  // browser finally answers — must NOT flip the already-settled result / re-resolve
  ok({ coords: { latitude: 3.1, longitude: 101.7, accuracy: 20 }, timestamp: Date.now() });
  await delay(20);
  assert.equal(r.status, "TIMEOUT"); // unchanged
});

test("getCurrentLocation: a LATE error after the watchdog fired is ignored", async (t) => {
  t.after(resetEnv);
  let err;
  setNavigator({
    geolocation: {
      getCurrentPosition(_ok, error) {
        err = error;
      },
    },
  });
  const r = await getCurrentLocation({ watchdogMs: 30 });
  assert.equal(r.status, "TIMEOUT");
  err({ code: 1 }); // late PERMISSION_DENIED
  await delay(20);
  assert.equal(r.status, "TIMEOUT");
});

test("getCurrentLocation: a fix that arrives BEFORE the watchdog wins (watchdog is cleared)", async (t) => {
  t.after(resetEnv);
  setNavigator({
    geolocation: {
      getCurrentPosition(ok) {
        setTimeout(() => ok({ coords: { latitude: 3.1, longitude: 101.7, accuracy: 10 }, timestamp: Date.now() }), 5);
      },
    },
  });
  const r = await getCurrentLocation({ watchdogMs: 60 });
  assert.equal(r.status, "AVAILABLE");
  await delay(80); // watchdog window passes — nothing else happens, no throw
  assert.equal(r.status, "AVAILABLE");
});

test("getCurrentLocation: high-accuracy retry is bounded by the same watchdog", async (t) => {
  t.after(resetEnv);
  let seenOpts;
  setNavigator({
    geolocation: {
      getCurrentPosition(_ok, _err, options) {
        seenOpts = options; // never answers
      },
    },
  });
  const r = await getCurrentLocation({ highAccuracy: true, watchdogMs: 40 });
  assert.equal(seenOpts.enableHighAccuracy, true);
  assert.equal(r.status, "TIMEOUT");
});

test("LOCATION_WATCHDOG_MS is a sane bound above the browser timeout", () => {
  assert.equal(typeof LOCATION_WATCHDOG_MS, "number");
  assert.ok(LOCATION_WATCHDOG_MS > LOCATION_TIMEOUT_MS);
  assert.ok(LOCATION_WATCHDOG_MS <= 30_000);
});

test("location.ts client timers use no Node-only APIs (.unref)", () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../location.ts"), "utf8");
  assert.ok(!src.includes(".unref"), "no .unref() in client timer code");
});

// --------------------------------------------------------------------------- //
// constants + "no hardcoded fallback location" guard
// --------------------------------------------------------------------------- //
test("policy constants have sane values", () => {
  assert.equal(LOCATION_TIMEOUT_MS, 15_000);
  assert.equal(LOCATION_MAX_AGE_MS, 120_000);
  assert.equal(LOCATION_HIGH_ACCURACY_DEFAULT, false);
  assert.ok(LOCATION_COARSE_ACCURACY_M >= 1_000);
});

test("location.ts contains NO hardcoded fallback coordinates", () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../location.ts"), "utf8");
  assert.ok(!src.includes("2.9213"), "Cyberjaya latitude must not appear");
  assert.ok(!src.includes("101.6559"), "Cyberjaya longitude must not appear");
  assert.ok(!/Cyberjaya/i.test(src));
  assert.ok(!/fallback(Lat|Lng|Location)/i.test(src));
});
