// B7 — pure view helpers for the Plan task-conflict surface. No React, no fetch.
import type { TaskConflict } from "../lib/types";

export type TaskUiState =
  | "idle"
  | "keeping"
  | "kept"
  | "searching"
  | "candidates"
  | "confirming"
  | "moved"
  | "stale"
  | "error"
  | "no_alt";

export function unresolvedTaskConflicts(conflicts: TaskConflict[] | undefined): TaskConflict[] {
  return (conflicts ?? []).filter((c) => !c.decision);
}

export function taskConflictKey(c: TaskConflict): string {
  return c.fingerprint;
}

export function isTaskConflictBusy(s: TaskUiState): boolean {
  return s === "keeping" || s === "searching" || s === "confirming";
}

/** True when the event side is one occurrence of a recurring series. */
export function isOccurrenceScoped(c: TaskConflict): boolean {
  return c.scope === "occurrence" || !!c.occurrence_date;
}

const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-21" -> "Mon 21 Sep". Empty string on bad input. */
export function occurrenceLabel(isoDate: string | null | undefined): string {
  if (!isoDate) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return "";
  const y = Number(m[1]), mo = Number(m[2]), day = Number(m[3]);
  const d = new Date(Date.UTC(y, mo - 1, day));
  if (
    Number.isNaN(d.getTime()) ||
    d.getUTCFullYear() !== y || d.getUTCMonth() !== mo - 1 || d.getUTCDate() !== day
  ) return "";
  return `${WD[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]}`;
}

/** "2:30 PM" from an ISO string carrying a time. */
export function hhmm(iso: string | null | undefined): string {
  if (!iso) return "";
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return "";
  let h = Number(m[1]);
  const min = Number(m[2]);
  const ap = h < 12 ? "AM" : "PM";
  h = h % 12 || 12;
  return min ? `${h}:${String(min).padStart(2, "0")} ${ap}` : `${h}:00 ${ap}`;
}

export function windowLabel(startIso: string, endIso: string): string {
  const a = hhmm(startIso), b = hhmm(endIso);
  return a && b ? `${a} – ${b}` : a || b;
}

/** "Tue 15 Sep 2026" from an ISO datetime string (reads the +08:00 date parts). */
export function dateLabel(iso: string | null | undefined): string {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return "";
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  if (Number.isNaN(d.getTime())) return "";
  return `${WD[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
