// Plan (Figma 238:130) — pure, derived-only display helpers for the day
// header ("4 plans · Free after 6 PM") and the timeline's "Free" gap slots.
// Everything here is a presentational DERIVATION from the real events/tasks
// already fetched for the selected day — no new data, no new decision, no
// backend involvement. A gap is purely informational (never interactive,
// never persisted) so this never introduces a new product capability.

export interface TimedSpan {
  id: string;
  start: string;
  end: string;
}

/** "4 plans" / "1 plan" / "Nothing planned" from the real counts. */
export function planCountLabel(eventCount: number, taskCount: number): string {
  const n = eventCount + taskCount;
  if (n === 0) return "Nothing planned";
  return `${n} plan${n === 1 ? "" : "s"}`;
}

/** The end time of the LAST real timed event of the day, or null when there
 * is none (so the caller can omit "Free after …" entirely rather than
 * inventing one). All-day events are excluded — they don't occupy a clock
 * slot to be "free after". */
export function lastTimedEventEnd(events: Array<{ end: string; all_day: boolean }>): string | null {
  let latest: string | null = null;
  for (const e of events) {
    if (e.all_day || !e.end) continue;
    if (latest === null || e.end > latest) latest = e.end;
  }
  return latest;
}

const MIN_GAP_MINUTES = 45;

/**
 * Real, computed open slots strictly BETWEEN consecutive timed events of the
 * day (never before the first or after the last — the day may continue past
 * the visible timeline). `events` must already be sorted by start ascending.
 * A gap shorter than MIN_GAP_MINUTES is not worth a card and is omitted.
 */
export function timelineGaps(events: TimedSpan[]): Array<{ afterId: string; start: string; end: string; minutes: number }> {
  const gaps: Array<{ afterId: string; start: string; end: string; minutes: number }> = [];
  for (let i = 0; i < events.length - 1; i++) {
    const cur = events[i];
    const next = events[i + 1];
    const curEnd = Date.parse(cur.end);
    const nextStart = Date.parse(next.start);
    if (Number.isNaN(curEnd) || Number.isNaN(nextStart) || nextStart <= curEnd) continue;
    const minutes = Math.round((nextStart - curEnd) / 60000);
    if (minutes < MIN_GAP_MINUTES) continue;
    gaps.push({ afterId: cur.id, start: cur.end, end: next.start, minutes });
  }
  return gaps;
}

/** "2 hours open" / "45 min open" — never fabricated, always from real minutes. */
export function gapDurationLabel(minutes: number): string {
  if (minutes < 60) return `${minutes} min open`;
  const hours = minutes / 60;
  const rounded = Math.round(hours * 10) / 10;
  const whole = Number.isInteger(rounded);
  return `${whole ? rounded : rounded.toFixed(1)} hour${rounded === 1 ? "" : "s"} open`;
}
