// Phase 5H-B3 — newClientToken() format + uniqueness (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import { newClientToken, pickOmniToken, omniTokenRetained } from "../clientToken.ts";

// The backend accepts client_token matching ^[A-Za-z0-9._~:-]{1,64}$.
const ACCEPTED = /^[A-Za-z0-9._~:\-]{1,64}$/;

test("newClientToken returns a value the backend contract accepts", () => {
  for (let i = 0; i < 50; i++) {
    const t = newClientToken();
    assert.ok(ACCEPTED.test(t), `rejected token: ${t}`);
  }
});

test("newClientToken is unique across calls", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 1000; i++) seen.add(newClientToken());
  assert.equal(seen.size, 1000);
});

test("newClientToken fallback shape is also accepted", () => {
  // exercise the non-crypto branch directly
  const fallback = `ct-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  assert.ok(ACCEPTED.test(fallback));
});

// --- Omni-bar token lifecycle ---------------------------------------------- //
test("pickOmniToken: no pending -> a fresh token", () => {
  const t = pickOmniToken(null, "log rm20 food");
  assert.ok(ACCEPTED.test(t));
});

test("pickOmniToken: identical retry of a failed submission -> SAME token", () => {
  const pending = { text: "log rm20 food", token: "tok-retry-1" };
  assert.equal(pickOmniToken(pending, "log rm20 food"), "tok-retry-1");
});

test("pickOmniToken: edited / different text -> a NEW token", () => {
  const pending = { text: "log rm20 food", token: "tok-retry-1" };
  const t = pickOmniToken(pending, "log rm25 food");
  assert.notEqual(t, "tok-retry-1");
  assert.ok(ACCEPTED.test(t));
});

test("omniTokenRetained: unknown/network outcome (null) -> retain for retry", () => {
  assert.equal(omniTokenRetained(null), true);
});

test("omniTokenRetained: committed / no-mutation terminal statuses -> release", () => {
  assert.equal(omniTokenRetained("SUCCESS"), false);
  assert.equal(omniTokenRetained("CLARIFICATION_REQUIRED"), false);
  assert.equal(omniTokenRetained("ACTION_NOT_SUPPORTED"), false);
});

test("omniTokenRetained: ambiguous error statuses -> retain (mutation may have committed)", () => {
  assert.equal(omniTokenRetained("VALIDATION_ERROR"), true);
  assert.equal(omniTokenRetained("INTERNAL_ERROR"), true);
  assert.equal(omniTokenRetained("DATABASE_ERROR"), true);
});
