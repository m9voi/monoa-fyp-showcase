// B6 — per-occurrence conflict view helpers (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  occurrenceLabel as occLabelC,
  isOccurrenceScoped as isOccC,
} from "../conflictView.ts";
import {
  occurrenceLabel as occLabelW,
  isOccurrenceScoped as isOccW,
} from "../weatherConflictView.ts";
import type { ScheduleConflict, WeatherConflict } from "../../lib/types.ts";

test("occurrenceLabel formats an ISO date as 'Wkd D Mon' (both modules agree)", () => {
  assert.equal(occLabelC("2026-09-21"), "Mon 21 Sep");
  assert.equal(occLabelW("2026-09-21"), "Mon 21 Sep");
  assert.equal(occLabelC("2026-12-01"), "Tue 1 Dec");
});

test("occurrenceLabel is empty for missing / malformed input", () => {
  for (const v of [null, undefined, "", "nope", "2026-9-1", "2026-13-40"]) {
    assert.equal(occLabelC(v as never), "");
    assert.equal(occLabelW(v as never), "");
  }
});

test("isOccurrenceScoped: overlap conflict is per-occurrence when recurring", () => {
  const base = { event_ids: ["a", "b"], a: {} as never, b: {} as never };
  assert.equal(isOccC({ ...base, conflict_type: "event_overlap" } as ScheduleConflict), false);
  assert.equal(isOccC({ ...base, conflict_type: "recurring_overlap" } as ScheduleConflict), true);
  assert.equal(isOccC({ ...base, scope: "occurrence" } as ScheduleConflict), true);
});

test("isOccurrenceScoped: weather conflict is per-occurrence when it carries an occurrence_date", () => {
  const base = {
    event_id: "e1", fingerprint: "f", hazard_type: "thunderstorm", severity_tier: 3,
    headline: "…", event: { id: "e1", start: "", end: "" },
    forecast: { stale: false }, decision: null,
  } as WeatherConflict;
  assert.equal(isOccW(base), false);
  assert.equal(isOccW({ ...base, occurrence_date: "2026-09-21" }), true);
  assert.equal(isOccW({ ...base, scope: "occurrence" }), true);
});
