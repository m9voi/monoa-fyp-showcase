"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { X, MapPin, Repeat, Pencil, CheckCircle2, Circle, Link2, AlertTriangle, Navigation } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import { fmtLongDate, fmtTime } from "../lib/datetime";
import { linkedPlaceActions } from "./placeLink";
import type { EventOccurrence, CoreTask } from "../lib/types";

const RECUR_LABEL: Record<string, string> = {
  none: "", daily: "Repeats every day", weekly: "Repeats every week",
  weekdays: "Repeats every weekday",
};

export function EventDetail({
  occ,
  conflictWith,
  relatedTaskTitle,
  onClose,
  onEdit,
}: {
  occ: EventOccurrence;
  conflictWith: string[];
  relatedTaskTitle?: string | null;
  onClose: () => void;
  onEdit: () => void;
}) {
  const router = useRouter();
  // The ONE gate for structured place actions — null once the event's structured
  // link is gone (unlinked, or replaced by a manual free-text location edit).
  const placeActions = linkedPlaceActions(occ.place);
  return (
    <Shell title={occ.title} onClose={onClose}>
      <p className="text-sm text-secondary">{fmtLongDate(occ.start)}</p>
      <p className="text-sm text-primary">
        {occ.all_day ? "All day" : `${fmtTime(occ.start)} – ${fmtTime(occ.end)}`}
      </p>

      {placeActions && occ.place ? (
        // Phase 5G: a REAL linked place — identity-preserving actions, no raw
        // provider id / lat-lng / evidence shown to the user.
        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 space-y-2">
          <p className="flex items-start gap-2 text-sm text-primary">
            <MapPin size={15} className="text-emerald-400 shrink-0 mt-0.5" />
            <span className="min-w-0">
              {occ.place.name}
              {occ.place.address && <span className="block text-xs text-muted">{occ.place.address}</span>}
            </span>
          </p>
          <div className="flex gap-2">
            <Link
              href={placeActions.viewUrl}
              className="flex-1 text-center bg-white/10 hover:bg-white/15 text-white text-xs font-bold py-2 rounded-lg"
            >
              View on map
            </Link>
            <a
              href={placeActions.directionsUrl}
              target="_blank"
              rel="noreferrer"
              aria-label={`Open directions to ${occ.place.name} in an external map`}
              className="flex-1 inline-flex items-center justify-center gap-1.5 bg-strong text-strong-fg hover:opacity-90 text-xs font-bold py-2 rounded-lg"
            >
              <Navigation size={13} /> Directions
            </a>
          </div>
        </div>
      ) : occ.location ? (
        <button
          onClick={() => router.push(`/map?q=${encodeURIComponent(occ.location as string)}&from=schedule&title=${encodeURIComponent(occ.title)}`)}
          className="flex items-center gap-2 text-sm text-emerald-400 hover:text-emerald-300"
        >
          <MapPin size={15} /> {occ.location}
        </button>
      ) : null}

      {occ.recurrence && occ.recurrence !== "none" && (
        <p className="flex items-center gap-2 text-xs text-muted">
          <Repeat size={13} /> {RECUR_LABEL[occ.recurrence] || "Recurring"}
        </p>
      )}

      {relatedTaskTitle && (
        <p className="flex items-center gap-2 text-xs text-muted">
          <Link2 size={13} /> Linked task: {relatedTaskTitle}
        </p>
      )}

      {conflictWith.length > 0 && (
        <p className="flex items-start gap-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          <AlertTriangle size={14} className="shrink-0 mt-0.5" />
          Overlaps with {conflictWith.join(", ")}. Edit the time to resolve it.
        </p>
      )}

      {occ.notes && <p className="text-sm text-secondary whitespace-pre-wrap">{occ.notes}</p>}

      <button onClick={onEdit} className="w-full flex items-center justify-center gap-2 bg-white/10 hover:bg-white/15 rounded-xl py-2.5 text-sm font-medium text-white">
        <Pencil size={15} /> Edit event
      </button>
    </Shell>
  );
}

export function TaskDetail({
  task,
  onClose,
  onEdit,
  onChanged,
}: {
  task: CoreTask;
  onClose: () => void;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await apiFetch(`/api/tasks/${task.id}/${task.is_completed ? "uncomplete" : "complete"}`, { method: "POST" });
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't update the task.");
      setBusy(false);
    }
  };

  return (
    <Shell title={task.title} onClose={onClose}>
      <p className="text-xs uppercase tracking-widest text-muted">Task</p>
      {task.due_date && <p className="text-sm text-secondary">Due {fmtLongDate(task.due_date)}</p>}
      {task.planned_at && task.scheduled_end ? (
        <p className="text-sm text-secondary">
          Work block {fmtLongDate(task.planned_at)} · {fmtTime(task.planned_at)} – {fmtTime(task.scheduled_end)}
        </p>
      ) : task.planned_at ? (
        <p className="text-sm text-secondary">Planned for {fmtLongDate(task.planned_at)} · {fmtTime(task.planned_at)}</p>
      ) : null}
      {task.reminder_at && (
        <p className="text-sm text-secondary">
          Reminder {fmtLongDate(task.reminder_at)} · {fmtTime(task.reminder_at)}
          <span className="text-muted"> ({(task.reminder_status || "NONE").toLowerCase()})</span>
        </p>
      )}
      {task.notes && <p className="text-sm text-secondary whitespace-pre-wrap">{task.notes}</p>}

      {error && <p className="text-sm text-red-300">{error}</p>}

      <button
        onClick={toggle}
        disabled={busy}
        className={`w-full flex items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-medium disabled:opacity-50 ${
          task.is_completed ? "bg-white/10 text-primary" : "bg-emerald-500 text-black"
        }`}
      >
        {task.is_completed ? <Circle size={15} /> : <CheckCircle2 size={15} />}
        {busy ? "Saving…" : task.is_completed ? "Mark as not done" : "Mark as done"}
      </button>
      <button onClick={onEdit} className="w-full flex items-center justify-center gap-2 bg-white/10 hover:bg-white/15 rounded-xl py-2.5 text-sm font-medium text-white">
        <Pencil size={15} /> Edit task
      </button>
    </Shell>
  );
}

function Shell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      className="fixed inset-0 z-[100] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="w-full max-w-md bg-surface border border-white/10 rounded-t-3xl sm:rounded-3xl p-6 space-y-3 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-lg font-semibold text-white">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-white p-1 shrink-0"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}
