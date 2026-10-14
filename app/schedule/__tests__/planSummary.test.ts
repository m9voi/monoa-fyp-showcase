import { test } from "node:test";
import assert from "node:assert/strict";

import { planCountLabel, lastTimedEventEnd, timelineGaps, gapDurationLabel } from "../planSummary.ts";

test("planCountLabel handles zero/singular/plural honestly", () => {
  assert.equal(planCountLabel(0, 0), "Nothing planned");
  assert.equal(planCountLabel(1, 0), "1 plan");
  assert.equal(planCountLabel(0, 1), "1 plan");
  assert.equal(planCountLabel(2, 2), "4 plans");
});

test("lastTimedEventEnd ignores all-day events and picks the real latest end", () => {
  assert.equal(
    lastTimedEventEnd([
      { end: "2026-09-20T11:00:00+08:00", all_day: false },
      { end: "2026-09-20T18:00:00+08:00", all_day: false },
      { end: "2026-09-20T23:59:59+08:00", all_day: true },
    ]),
    "2026-09-20T18:00:00+08:00",
  );
});

test("lastTimedEventEnd is null for an empty or all-day-only day", () => {
  assert.equal(lastTimedEventEnd([]), null);
  assert.equal(lastTimedEventEnd([{ end: "2026-09-20T23:59:59+08:00", all_day: true }]), null);
});

test("timelineGaps finds a real gap between two consecutive timed events", () => {
  const gaps = timelineGaps([
    { id: "a", start: "2026-09-20T11:30:00+08:00", end: "2026-09-20T13:00:00+08:00" },
    { id: "b", start: "2026-09-20T16:00:00+08:00", end: "2026-09-20T17:00:00+08:00" },
  ]);
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].afterId, "a");
  assert.equal(gaps[0].minutes, 180);
});

test("timelineGaps omits a gap shorter than the minimum threshold", () => {
  const gaps = timelineGaps([
    { id: "a", start: "2026-09-20T11:00:00+08:00", end: "2026-09-20T12:00:00+08:00" },
    { id: "b", start: "2026-09-20T12:10:00+08:00", end: "2026-09-20T13:00:00+08:00" },
  ]);
  assert.equal(gaps.length, 0);
});

test("timelineGaps never fires before the first or after the last event", () => {
  const gaps = timelineGaps([{ id: "solo", start: "2026-09-20T09:00:00+08:00", end: "2026-09-20T10:00:00+08:00" }]);
  assert.equal(gaps.length, 0);
});

test("timelineGaps ignores overlapping/adjacent events (never a negative gap)", () => {
  const gaps = timelineGaps([
    { id: "a", start: "2026-09-20T09:00:00+08:00", end: "2026-09-20T11:00:00+08:00" },
    { id: "b", start: "2026-09-20T10:30:00+08:00", end: "2026-09-20T12:00:00+08:00" },
  ]);
  assert.equal(gaps.length, 0);
});

test("gapDurationLabel formats minutes vs hours honestly", () => {
  assert.equal(gapDurationLabel(45), "45 min open");
  assert.equal(gapDurationLabel(60), "1 hour open");
  assert.equal(gapDurationLabel(120), "2 hours open");
  assert.equal(gapDurationLabel(90), "1.5 hours open");
});
