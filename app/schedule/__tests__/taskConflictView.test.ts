// B7 — task-conflict view helpers (Node's built-in runner). `npm run test:node`.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  unresolvedTaskConflicts, taskConflictKey, isTaskConflictBusy,
  isOccurrenceScoped, occurrenceLabel, hhmm, windowLabel, dateLabel,
} from "../taskConflictView.ts";
import type { TaskConflict } from "../../lib/types.ts";

function mk(over: Partial<TaskConflict> = {}): TaskConflict {
  return {
    fingerprint: "abcdef0123456789abcdef0123456789",
    conflict_type: "task_event_overlap",
    scope: "series",
    occurrence_date: null,
    explanation: "…",
    task: { id: "t1", title: "Write report", start: "2026-09-12T14:30:00+08:00", end: "2026-09-12T15:30:00+08:00", due_date: "2026-09-15T23:59:00+08:00" },
    event: { id: "e1", title: "Board meeting", start: "2026-09-12T14:00:00+08:00", end: "2026-09-12T15:00:00+08:00", recurring: false },
    proposal: null,
    decision: null,
    ...over,
  };
}

test("unresolvedTaskConflicts drops decided ones", () => {
  assert.equal(unresolvedTaskConflicts([mk(), mk({ decision: "kept" }), mk({ decision: "moved" })]).length, 1);
  assert.deepEqual(unresolvedTaskConflicts(undefined), []);
});

test("taskConflictKey is the fingerprint", () => {
  assert.equal(taskConflictKey(mk()), "abcdef0123456789abcdef0123456789");
});

test("isTaskConflictBusy covers the in-flight states only", () => {
  for (const s of ["keeping", "searching", "confirming"] as const) assert.equal(isTaskConflictBusy(s), true);
  for (const s of ["idle", "kept", "candidates", "moved", "stale", "error", "no_alt"] as const) assert.equal(isTaskConflictBusy(s), false);
});

test("isOccurrenceScoped: true only when the event side is a recurring occurrence", () => {
  assert.equal(isOccurrenceScoped(mk()), false);
  assert.equal(isOccurrenceScoped(mk({ scope: "occurrence" })), true);
  assert.equal(isOccurrenceScoped(mk({ occurrence_date: "2026-09-21" })), true);
});

test("occurrenceLabel formats / rejects", () => {
  assert.equal(occurrenceLabel("2026-09-21"), "Mon 21 Sep");
  for (const v of [null, undefined, "", "nope", "2026-13-40"]) assert.equal(occurrenceLabel(v as never), "");
});

test("hhmm + windowLabel read the clock straight off the ISO offset string", () => {
  assert.equal(hhmm("2026-09-12T14:30:00+08:00"), "2:30 PM");
  assert.equal(hhmm("2026-09-12T09:00:00+08:00"), "9:00 AM");
  assert.equal(windowLabel("2026-09-12T14:30:00+08:00", "2026-09-12T15:30:00+08:00"), "2:30 PM – 3:30 PM");
});

test("dateLabel renders a full date and rejects junk (B7.1 deadline copy)", () => {
  assert.equal(dateLabel("2026-09-15T23:59:00+08:00"), "Tue 15 Sep 2026");
  assert.equal(dateLabel("2026-12-01T00:00:00+08:00"), "Tue 1 Dec 2026");
  for (const v of [null, undefined, "", "nope"]) assert.equal(dateLabel(v as never), "");
});
