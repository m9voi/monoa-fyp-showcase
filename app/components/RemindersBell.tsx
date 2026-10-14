"use client";

// B11.1 — the ONE lightweight in-app reminder surface. A signed-in user sees
// their own DUE, unacknowledged task reminders while using / reopening MONOA and
// can acknowledge (dismiss) them; the acknowledgement is persisted server-side
// (reminder_status -> ACKNOWLEDGED) so it never reappears. NO push, NO service
// worker, NO Notification permission request — this is in-app only.
//
// It is NOT a navigation tab: a small fixed bell button next to the profile
// avatar, shown only when there is at least one reminder AND the account's
// in-app reminders preference is on. Clicking it opens an accessible dialog.

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, X, Check } from "lucide-react";
import { useAuth } from "../lib/auth";
import { useSettings } from "./ThemeProvider";
import { apiFetch } from "../lib/api";
import { fmtDayLabel, fmtTime } from "../lib/datetime";
import { shouldShowReminders, reminderBadge, reminderDetail, applyLocalAcks } from "../lib/reminders";
import type { DueReminder, DueRemindersResponse } from "../lib/types";

const POLL_MS = 60_000;

function formatWhen(iso: string): string {
  const day = fmtDayLabel(iso);
  const time = fmtTime(iso);
  return day && time ? `${day}, ${time}` : day || time || "";
}

export default function RemindersBell() {
  const { status } = useAuth();
  const { notifications } = useSettings();
  const authed = status === "authenticated";

  const [serverList, setServerList] = useState<DueReminder[]>([]);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [ackedIds, setAckedIds] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    if (!authed) return;
    try {
      const data = await apiFetch<DueRemindersResponse>("/api/reminders/due", { kind: "crud" });
      if (!mountedRef.current) return;
      setServerList(Array.isArray(data.reminders) ? data.reminders : []);
      setNotificationsEnabled(data.notifications_enabled !== false);
      // drop local acks the server has now caught up on
      setAckedIds((prev) => {
        const live = new Set(data.reminders.map((r) => r.task_id));
        const next = new Set<string>();
        for (const id of prev) if (live.has(id)) next.add(id);
        return next;
      });
    } catch {
      /* transient — keep the last known list, try again on the next tick */
    }
  }, [authed]);

  // poll while signed in; refresh immediately when the tab regains focus so
  // coming back from Settings / another tab is up to date within a second.
  useEffect(() => {
    if (!authed) {
      // sync the surface to sign-out: nothing to poll, nothing to show
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setServerList([]);
      setAckedIds(new Set());
      setOpen(false);
      return;
    }
    void load();
    const iv = setInterval(() => void load(), POLL_MS);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      clearInterval(iv);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [authed, load]);

  const visible = useMemo(
    () => applyLocalAcks(serverList, ackedIds),
    [serverList, ackedIds],
  );
  // the account preference gates presentation on BOTH ends (server returns an
  // empty list when off; this is the client mirror so the toggle feels instant)
  const show = shouldShowReminders(visible, notifications && notificationsEnabled);
  // the dialog can never stay open once there is nothing to show
  const dialogOpen = open && show;

  const acknowledge = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setAckedIds((prev) => new Set(prev).add(taskId));   // optimistic, idempotent server-side
    try {
      await apiFetch(`/api/reminders/${encodeURIComponent(taskId)}/ack`, {
        method: "POST",
        kind: "crud",
      });
    } catch {
      // revert the optimistic removal so a real failure doesn't hide a reminder
      if (mountedRef.current) {
        setAckedIds((prev) => {
          const next = new Set(prev);
          next.delete(taskId);
          return next;
        });
      }
    } finally {
      if (mountedRef.current) {
        setBusyId(null);
        void load();
      }
    }
  }, [load]);

  // ---- accessible dialog: Escape to close, focus moved in, focus restored ----
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!dialogOpen) return;
    const opener = openerRef.current;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    const t = setTimeout(() => {
      dialogRef.current?.querySelector<HTMLElement>("button, [href], [tabindex]")?.focus();
    }, 0);
    return () => {
      document.removeEventListener("keydown", onKey);
      clearTimeout(t);
      opener?.focus();
    };
  }, [dialogOpen]);

  if (!authed || !show) return null;

  const count = visible.length;

  return (
    <>
      <button
        ref={openerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={dialogOpen}
        aria-label={`${count} due reminder${count === 1 ? "" : "s"} — open`}
        className="fixed z-50 flex h-10 w-10 items-center justify-center rounded-full border border-line-strong bg-surface text-primary shadow-sm backdrop-blur-md transition-transform active:scale-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring right-[3.75rem] lg:right-4"
        style={{ top: "max(0.75rem, env(safe-area-inset-top))" }}
      >
        <Bell size={18} aria-hidden="true" />
        <span className="absolute -right-0.5 -top-0.5 min-w-[16px] rounded-full bg-danger px-1 text-[10px] font-bold leading-4 text-strong-fg">
          {reminderBadge(count)}
        </span>
      </button>

      <p aria-live="polite" className="sr-only">
        {count} due reminder{count === 1 ? "" : "s"}
      </p>

      {dialogOpen && (
        <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/40 p-4 pt-[max(1rem,env(safe-area-inset-top))]">
          <button
            type="button"
            aria-label="Close reminders"
            onClick={() => setOpen(false)}
            className="absolute inset-0 h-full w-full cursor-default"
            tabIndex={-1}
          />
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="reminders-title"
            className="relative z-[61] w-full max-w-sm rounded-2xl border border-line bg-app p-4 shadow-xl"
          >
            <div className="flex items-center justify-between">
              <h2 id="reminders-title" className="text-sm font-semibold text-primary">
                Due reminders
              </h2>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="flex h-11 w-11 items-center justify-center rounded-full text-muted hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              >
                <X size={18} aria-hidden="true" />
              </button>
            </div>

            <ul className="mt-2 space-y-2">
              {visible.map((r) => (
                <li
                  key={r.task_id}
                  className="rounded-xl border border-line bg-surface p-3"
                >
                  <p className="text-sm font-medium text-primary break-words">{r.title}</p>
                  <p className="mt-0.5 text-xs text-muted">{reminderDetail(r, formatWhen)}</p>
                  <button
                    type="button"
                    onClick={() => void acknowledge(r.task_id)}
                    disabled={busyId === r.task_id}
                    aria-label={`Acknowledge reminder for ${r.title}`}
                    className="mt-2 inline-flex min-h-[44px] items-center gap-2 rounded-xl bg-strong px-4 text-sm font-bold text-strong-fg hover:opacity-90 disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                  >
                    <Check size={16} aria-hidden="true" />
                    {busyId === r.task_id ? "Dismissing…" : "Got it"}
                  </button>
                </li>
              ))}
            </ul>

            <p className="mt-3 text-[10px] text-muted">
              Shown only while MONOA is open. There is no background or closed-app notification.
            </p>
          </div>
        </div>
      )}
    </>
  );
}
