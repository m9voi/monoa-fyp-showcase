// B3 — Plan conflict resolver view helpers (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  unresolvedConflicts,
  conflictKey,
  hhmm,
  spanLabel,
  shiftLabel,
  isBusy,
  isSettled,
} from "../conflictView.ts";
import type { ScheduleConflict } from "../../lib/types.ts";

function mkConflict(over: Partial<ScheduleConflict> = {}): ScheduleConflict {
  return {
    event_ids: ["e2", "e1"],
    // minimal EventOccurrence-ish stubs — only fields the helpers read
    a: { id: "e1", title: "Design Review", start: "2026-09-11T14:00:00+08:00", end: "2026-09-11T15:00:00+08:00" } as never,
    b: { id: "e2", title: "Team Sync", start: "2026-09-11T14:30:00+08:00", end: "2026-09-11T15:30:00+08:00" } as never,
    conflict_type: "event_overlap",
    decidable: true,
    fingerprint: "abcdef0123456789abcdef0123456789",
    explanation: "…",
    proposal: {
      event_id: "e2",
      event_title: "Team Sync",
      current_start: "2026-09-11T14:30:00+08:00",
      current_end: "2026-09-11T15:30:00+08:00",
      start: "2026-09-11T15:00:00+08:00",
      end: "2026-09-11T16:00:00+08:00",
    },
    decision: null,
    ...over,
  };
}

test("unresolvedConflicts drops any conflict with a recorded decision", () => {
  const open = mkConflict();
  const kept = mkConflict({ fingerprint: "f2", decision: "kept" });
  const accepted = mkConflict({ fingerprint: "f3", decision: "accepted" });
  const out = unresolvedConflicts([open, kept, accepted]);
  assert.equal(out.length, 1);
  assert.equal(out[0].fingerprint, "abcdef0123456789abcdef0123456789");
});

test("unresolvedConflicts tolerates undefined", () => {
  assert.deepEqual(unresolvedConflicts(undefined), []);
});

test("conflictKey prefers the fingerprint, else a sorted id pair", () => {
  assert.equal(conflictKey(mkConflict()), "abcdef0123456789abcdef0123456789");
  assert.equal(
    conflictKey(mkConflict({ fingerprint: null })),
    "e1~e2",
  );
});

test("hhmm reads clock parts straight off the +08:00 ISO string", () => {
  assert.equal(hhmm("2026-09-11T14:00:00+08:00"), "2:00 PM");
  assert.equal(hhmm("2026-09-11T15:30:00+08:00"), "3:30 PM");
  assert.equal(hhmm("2026-09-11T00:05:00+08:00"), "12:05 AM");
  assert.equal(hhmm("2026-09-11T12:00:00+08:00"), "12:00 PM");
  assert.equal(hhmm(null), "");
  assert.equal(hhmm("not-a-date"), "");
});

test("spanLabel joins start and end", () => {
  assert.equal(
    spanLabel("2026-09-11T14:00:00+08:00", "2026-09-11T15:00:00+08:00"),
    "2:00 PM – 3:00 PM",
  );
});

test("shiftLabel describes the proposed move, or null with no proposal", () => {
  assert.equal(
    shiftLabel(mkConflict().proposal),
    "Move “Team Sync” to 3:00 PM – 4:00 PM",
  );
  assert.equal(shiftLabel(null), null);
  assert.equal(shiftLabel(undefined), null);
});

test("isBusy / isSettled classify the UI state", () => {
  assert.equal(isBusy("accepting"), true);
  assert.equal(isBusy("keeping"), true);
  assert.equal(isBusy("idle"), false);
  assert.equal(isSettled("accepted"), true);
  assert.equal(isSettled("kept"), true);
  assert.equal(isSettled("stale"), false);
});
