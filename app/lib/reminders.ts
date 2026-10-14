// B11.1 — pure helpers for the in-app DUE reminder surface. No DOM, no React,
// no network. Unit-tested with `npm run test:node`.

import type { DueReminder } from "./types";

/**
 * Whether the reminders bell/surface should be shown at all. It is meaningful
 * ONLY when the account's in-app reminders preference is on AND there is at
 * least one delivered, unacknowledged reminder. An empty or suppressed list
 * renders nothing (no empty badge, no fake "0").
 */
export function shouldShowReminders(
  reminders: readonly DueReminder[] | null | undefined,
  notificationsEnabled: boolean,
): boolean {
  return notificationsEnabled === true && Array.isArray(reminders) && reminders.length > 0;
}

/** Badge count text — capped so a runaway list never breaks the layout. */
export function reminderBadge(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return "";
  return count > 9 ? "9+" : String(Math.floor(count));
}

/**
 * A short, factual line for one reminder row. Never invents information: it
 * states the real reminder time, and the real due date only when the task has
 * one. `formatWhen` is injected so the component can pass its locale/timezone
 * formatter and this stays pure + testable.
 */
export function reminderDetail(
  r: Pick<DueReminder, "reminder_at" | "due_date">,
  formatWhen: (iso: string) => string,
): string {
  const when = r.reminder_at ? formatWhen(r.reminder_at) : "";
  const base = when ? `Reminder set for ${when}` : "Reminder";
  if (r.due_date) {
    const due = formatWhen(r.due_date);
    return due ? `${base} · due ${due}` : base;
  }
  return base;
}

/** Merge a fresh server list with what is on screen, dropping anything the
 *  viewer just acknowledged locally (so it never flashes back before the next
 *  fetch). Order follows the server (already `reminder_at ASC`). */
export function applyLocalAcks(
  serverList: readonly DueReminder[],
  acknowledgedIds: ReadonlySet<string>,
): DueReminder[] {
  return serverList.filter((r) => !acknowledgedIds.has(r.task_id));
}
