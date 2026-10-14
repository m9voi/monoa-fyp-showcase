// B11.1 — in-app reminder pure helpers. Run via `npm run test:node`.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  shouldShowReminders,
  reminderBadge,
  reminderDetail,
  applyLocalAcks,
} from "../reminders.ts";
import type { DueReminder } from "../types.ts";

const r = (id: string, extra: Partial<DueReminder> = {}): DueReminder => ({
  task_id: id,
  title: `Task ${id}`,
  reminder_at: "2026-09-11T09:00:00+08:00",
  ...extra,
});

test("shouldShowReminders: needs the preference ON and at least one reminder", () => {
  assert.equal(shouldShowReminders([r("1")], true), true);
  assert.equal(shouldShowReminders([r("1")], false), false); // preference off -> hidden
  assert.equal(shouldShowReminders([], true), false); // nothing due -> no empty badge
  assert.equal(shouldShowReminders(null, true), false);
  assert.equal(shouldShowReminders(undefined, true), false);
});

test("reminderBadge: 1..9 verbatim, >9 capped, junk -> empty", () => {
  assert.equal(reminderBadge(1), "1");
  assert.equal(reminderBadge(9), "9");
  assert.equal(reminderBadge(10), "9+");
  assert.equal(reminderBadge(0), "");
  assert.equal(reminderBadge(-3), "");
  assert.equal(reminderBadge(Number.NaN), "");
});

test("reminderDetail: states the real reminder time, and due only when present", () => {
  const f = (iso: string) => (iso === "2026-09-11T09:00:00+08:00" ? "Fri, 9:00 AM" : "Sat, 10:00 AM");
  assert.equal(reminderDetail(r("1"), f), "Reminder set for Fri, 9:00 AM");
  assert.equal(
    reminderDetail(r("2", { due_date: "2026-09-12T10:00:00+08:00" }), f),
    "Reminder set for Fri, 9:00 AM · due Sat, 10:00 AM",
  );
  // a formatter that can't parse -> graceful, no fabricated text
  assert.equal(reminderDetail({ reminder_at: "", due_date: null }, () => ""), "Reminder");
});

test("applyLocalAcks: filters out ids the viewer just acknowledged, keeps order", () => {
  const list = [r("a"), r("b"), r("c")];
  assert.deepEqual(
    applyLocalAcks(list, new Set(["b"])).map((x) => x.task_id),
    ["a", "c"],
  );
  assert.deepEqual(applyLocalAcks(list, new Set()).map((x) => x.task_id), ["a", "b", "c"]);
  assert.deepEqual(applyLocalAcks(list, new Set(["a", "b", "c"])), []);
});
