// Phase 5G — "Add to Schedule" pure-helper tests (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  prefillTitle,
  roundedWindow,
  localInputToMyt,
  draftError,
  buildCreateBody,
} from "../addToSchedule.ts";

const REF = {
  provider: "google",
  provider_place_id: "ChIJ_x",
  place_id: "google:ChIJ_x",
  name: "Elite Futsal Arena",
  address: "1 Jalan X",
  latitude: 3.16,
  longitude: 101.71,
};

test("prefillTitle: activity intent + place", () => {
  assert.equal(prefillTitle({ placeName: "Elite Futsal Arena", activityLabel: "Futsal" }), "Futsal at Elite Futsal Arena");
});

test("prefillTitle: place only when no activity", () => {
  assert.equal(prefillTitle({ placeName: "Elite Futsal Arena" }), "Elite Futsal Arena");
  assert.equal(prefillTitle({ placeName: "Elite Futsal Arena", activityLabel: "" }), "Elite Futsal Arena");
});

test("roundedWindow: next full hour (MYT), one hour long", () => {
  // 2026-09-03 14:23:00 MYT  ->  06:23 UTC
  const now = Date.UTC(2026, 8, 3, 6, 23, 0);
  const { startInput, endInput } = roundedWindow(now);
  assert.equal(startInput, "2026-09-03T15:00");
  assert.equal(endInput, "2026-09-03T16:00");
});

test("roundedWindow: rolls the date over midnight MYT", () => {
  // 2026-09-03 23:30 MYT -> start 2026-09-04 00:00, end 01:00
  const now = Date.UTC(2026, 8, 3, 15, 30, 0);
  const { startInput, endInput } = roundedWindow(now);
  assert.equal(startInput, "2026-09-04T00:00");
  assert.equal(endInput, "2026-09-04T01:00");
});

test("localInputToMyt appends the +08:00 offset (no tz redesign)", () => {
  assert.equal(localInputToMyt("2026-09-03T15:00"), "2026-09-03T15:00:00+08:00");
  assert.equal(localInputToMyt(""), "");
});

test("draftError catches the obvious problems before hitting the backend", () => {
  assert.match(draftError({ title: "  ", startInput: "2026-09-03T15:00", endInput: "2026-09-03T16:00" }) || "", /title/i);
  assert.match(draftError({ title: "x", startInput: "", endInput: "2026-09-03T16:00" }) || "", /start/i);
  assert.match(draftError({ title: "x", startInput: "2026-09-03T15:00", endInput: "" }) || "", /end/i);
  assert.match(
    draftError({ title: "x", startInput: "2026-09-03T16:00", endInput: "2026-09-03T15:00" }) || "",
    /after/i,
  );
  assert.equal(draftError({ title: "x", startInput: "2026-09-03T15:00", endInput: "2026-09-03T16:00" }), null);
});

test("buildCreateBody: provider-neutral place ref + a client_token, no volatile facts", () => {
  const body = buildCreateBody({
    title: "  Futsal at Elite Futsal Arena  ",
    startInput: "2026-09-03T15:00",
    endInput: "2026-09-03T16:00",
    ref: REF,
    clientToken: "tok-123",
  });
  assert.deepEqual(body, {
    title: "Futsal at Elite Futsal Arena",
    start: "2026-09-03T15:00:00+08:00",
    end: "2026-09-03T16:00:00+08:00",
    all_day: false,
    place: {
      provider: "google",
      provider_place_id: "ChIJ_x",
      name: "Elite Futsal Arena",
      address: "1 Jalan X",
      latitude: 3.16,
      longitude: 101.71,
    },
    client_token: "tok-123",
  });
  // the body must not smuggle rating / open_now / hours into `place`
  const placeKeys = Object.keys((body as { place: Record<string, unknown> }).place);
  for (const k of ["rating", "rating_count", "open_now", "opening_hours", "business_status"]) {
    assert.ok(!placeKeys.includes(k));
  }
});

test("buildCreateBody: the same token is reused for a retry (idempotency contract)", () => {
  const args = {
    title: "x",
    startInput: "2026-09-03T15:00",
    endInput: "2026-09-03T16:00",
    ref: REF,
    clientToken: "stable-token",
  };
  const a = buildCreateBody(args) as { client_token: string };
  const b = buildCreateBody(args) as { client_token: string };
  assert.equal(a.client_token, b.client_token);
});
