// Pure view helpers for the Plan conflict resolver (B3). No React, no fetch —
// unit-tested in isolation. All timestamps from the backend carry the MYT
// (+08:00) offset, so clock parts are read straight off the ISO string rather
// than through the platform's timezone database.
import type { ScheduleConflict, ConflictProposal } from "../lib/types";

export function unresolvedConflicts(conflicts: ScheduleConflict[] | undefined): ScheduleConflict[] {
  return (conflicts ?? []).filter((c) => !c.decision);
}

/** Stable per-conflict key for React lists and in-flight tracking. */
export function conflictKey(c: ScheduleConflict): string {
  return c.fingerprint ?? [...c.event_ids].sort().join("~");
}

/** "3:30 PM" from an ISO-8601 string that includes a time. Empty string if unparseable. */
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

/** "2:00 PM – 3:00 PM" */
export function spanLabel(startIso: string, endIso: string): string {
  const a = hhmm(startIso);
  const b = hhmm(endIso);
  return a && b ? `${a} – ${b}` : a || b;
}

/** The one-line description of the proposed reschedule, or null when no proposal. */
export function shiftLabel(p: ConflictProposal | null | undefined): string | null {
  if (!p) return null;
  const title = (p.event_title || "the later event").trim();
  return `Move “${title}” to ${spanLabel(p.start, p.end)}`;
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

/** True when a conflict's decision would change only one recurring occurrence. */
export function isOccurrenceScoped(c: ScheduleConflict): boolean {
  return c.scope === "occurrence" || c.conflict_type === "recurring_overlap";
}

export type ConflictUiState =
  | "idle"
  | "accepting"
  | "accepted"
  | "keeping"
  | "kept"
  | "stale"
  | "error";

export function isBusy(s: ConflictUiState): boolean {
  return s === "accepting" || s === "keeping";
}

export function isSettled(s: ConflictUiState): boolean {
  return s === "accepted" || s === "kept";
}
