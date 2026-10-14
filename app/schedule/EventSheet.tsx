"use client";

import React, { useState } from "react";
import { X, Trash2 } from "lucide-react";
import { apiFetch, ApiError, mutationOutcome, describeFailure } from "../lib/api";
import { newClientToken } from "../lib/clientToken";
import { usePendingCreate } from "../lib/usePendingCreate";
import { isoToLocalInput, localInputToIso } from "../lib/datetime";
import type { CoreEvent, CoreTask } from "../lib/types";

type Mode = "create" | "edit";

const RECURRENCE = [
  { v: "none", label: "Does not repeat" },
  { v: "daily", label: "Every day" },
  { v: "weekdays", label: "Every weekday (Mon–Fri)" },
  { v: "weekly", label: "Every week" },
];

export default function EventSheet({
  mode,
  initial,
  defaultDateKey,
  tasks,
  onClose,
  onSaved,
}: {
  mode: Mode;
  initial?: CoreEvent | null;
  defaultDateKey?: string;
  tasks: CoreTask[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const defStart = defaultDateKey ? `${defaultDateKey}T09:00` : isoToLocalInput(new Date().toISOString());
  const [title, setTitle] = useState(initial?.title ?? "");
  const [allDay, setAllDay] = useState(Boolean(initial?.all_day));
  const [start, setStart] = useState(isoToLocalInput(initial?.start_time) || defStart);
  const [end, setEnd] = useState(isoToLocalInput(initial?.end_time) || "");
  const [location, setLocation] = useState(initial?.location ?? "");
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [recurrence, setRecurrence] = useState(initial?.recurrence ?? "none");
  const [environment, setEnvironment] = useState<"indoor" | "outdoor" | "uncertain">(
    initial?.environment ?? "uncertain",
  );
  const [until, setUntil] = useState(
    initial?.recurrence_until ? isoToLocalInput(initial.recurrence_until).slice(0, 10) : "",
  );
  const [relatedTask, setRelatedTask] = useState(initial?.related_task_id ?? "");
  const [busy, setBusy] = useState<null | "save" | "delete">(null);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Phase 5H-B3: one idempotency token per open form -> a retried submit cannot
  // create a second event. (The Map/Activity -> Schedule bridge has its own.)
  const [clientToken] = useState<string>(newClientToken);
  const pending = usePendingCreate("event", clientToken);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (!title.trim()) {
      setError("Give the event a title.");
      return;
    }
    if (!start) {
      setError("Pick a start time.");
      return;
    }
    setBusy("save");
    setError("");
    const body: Record<string, unknown> = {
      title: title.trim(),
      start: localInputToIso(start),
      end: end ? localInputToIso(end) : null,
      all_day: allDay,
      location: location.trim() || null,
      notes: notes.trim() || null,
      recurrence,
      recurrence_until: recurrence !== "none" && until ? localInputToIso(`${until}T23:59`) : null,
      related_task_id: relatedTask || null,
      environment,
    };
    try {
      if (mode === "create") {
        pending.arm();
        await apiFetch("/api/events", {
          method: "POST",
          body: JSON.stringify({ ...body, client_token: clientToken }),
        });
        pending.resolve();
      } else if (initial) {
        await apiFetch(`/api/events/${initial.id}`, { method: "PATCH", body: JSON.stringify(body) });
      }
      onSaved();
      onClose();
    } catch (err) {
      const is409 = err instanceof ApiError && err.kind === "http" && err.status === 409;
      if (mode === "create" && is409) {
        setError(
          "This was already saved a moment ago, with different details. Check your schedule and refresh if it looks wrong.",
        );
      } else if (mode === "create" && mutationOutcome(err) === "unknown") {
        setError(
          "We couldn't confirm whether this was saved. Check your schedule — it may already be there — before adding it again.",
        );
      } else {
        if (mode === "create") pending.resolve();
        setError(describeFailure(err, { verb: "save this" }));
      }
      setBusy(null);
    }
  };

  const doDelete = async () => {
    if (!initial || busy) return;
    setBusy("delete");
    try {
      await apiFetch(`/api/events/${initial.id}`, { method: "DELETE" });
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't delete. Please try again.");
      setBusy(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[100] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={mode === "create" ? "Add event" : "Edit event"}
    >
      <div
        className="w-full max-w-md bg-surface border border-white/10 rounded-t-3xl sm:rounded-3xl p-6 space-y-4 max-h-[90vh] overflow-y-auto"
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">{mode === "create" ? "Add event" : "Edit event"}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-white p-1"><X size={18} /></button>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <label className="block">
            <span className="text-xs text-muted">Title</span>
            <input
              type="text" required maxLength={200} value={title}
              onChange={(e) => setTitle(e.target.value)} autoFocus
              placeholder="e.g. FYP presentation"
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-secondary">
            <input type="checkbox" checked={allDay} onChange={(e) => setAllDay(e.target.checked)} className="accent-emerald-500" />
            All-day
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs text-muted">Start</span>
              <input
                type={allDay ? "date" : "datetime-local"}
                required
                value={allDay ? start.slice(0, 10) : start}
                onChange={(e) => setStart(allDay ? `${e.target.value}T00:00` : e.target.value)}
                className="mt-1 w-full bg-field border border-field-border rounded-xl px-2 py-2.5 text-sm text-white outline-none focus:border-ring"
              />
            </label>
            {!allDay && (
              <label className="block">
                <span className="text-xs text-muted">End</span>
                <input
                  type="datetime-local" value={end}
                  onChange={(e) => setEnd(e.target.value)}
                  className="mt-1 w-full bg-field border border-field-border rounded-xl px-2 py-2.5 text-sm text-white outline-none focus:border-ring"
                />
              </label>
            )}
          </div>

          <label className="block">
            <span className="text-xs text-muted">Location (optional)</span>
            <input
              type="text" maxLength={200} value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="e.g. Tamarind Square"
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
            />
          </label>

          <label className="block">
            <span className="text-xs text-muted">Notes (optional)</span>
            <textarea
              rows={2} maxLength={2000} value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring resize-none"
            />
          </label>

          <label className="block">
            <span className="text-xs text-muted">Repeat</span>
            <select
              value={recurrence}
              onChange={(e) => setRecurrence(e.target.value as CoreEvent["recurrence"])}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
            >
              {RECURRENCE.map((r) => <option key={r.v} value={r.v}>{r.label}</option>)}
            </select>
          </label>

          <label className="block">
            <span className="text-xs text-muted">Indoor or outdoor?</span>
            <select
              value={environment}
              onChange={(e) => setEnvironment(e.target.value as "indoor" | "outdoor" | "uncertain")}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
            >
              <option value="uncertain">Not sure</option>
              <option value="outdoor">Outdoor</option>
              <option value="indoor">Indoor</option>
            </select>
            <span className="mt-1 block text-[11px] text-faint">
              Outdoor events may show a weather warning near the time.
            </span>
          </label>

          {recurrence !== "none" && (
            <label className="block">
              <span className="text-xs text-muted">Repeat until (optional)</span>
              <input
                type="date" value={until}
                onChange={(e) => setUntil(e.target.value)}
                className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
              />
            </label>
          )}

          {tasks.length > 0 && (
            <label className="block">
              <span className="text-xs text-muted">Linked task (optional)</span>
              <select
                value={relatedTask}
                onChange={(e) => setRelatedTask(e.target.value)}
                className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
              >
                <option value="">None</option>
                {tasks.filter((t) => !t.is_completed).map((t) => (
                  <option key={t.id} value={t.id}>{t.title}</option>
                ))}
              </select>
            </label>
          )}

          {mode === "create" && pending.recovered && (
            <p className="text-sm text-amber-200 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2">
              An event you started earlier had already been saved. You may not need to add this one.
            </p>
          )}

          {error && (
            <p className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</p>
          )}

          <button
            type="submit" disabled={busy !== null}
            className="w-full bg-white text-black font-semibold py-3 rounded-xl disabled:opacity-50"
          >
            {busy === "save" ? "Saving…" : mode === "create" ? "Add event" : "Save changes"}
          </button>
        </form>

        {mode === "edit" && initial && (
          <div className="pt-2 border-t border-white/5">
            {!confirmDelete ? (
              <button onClick={() => setConfirmDelete(true)} className="w-full flex items-center justify-center gap-2 text-red-400 hover:text-red-300 text-sm py-2">
                <Trash2 size={15} /> Delete event
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-secondary text-center">Delete this event? This cannot be undone.</p>
                <div className="flex gap-2">
                  <button onClick={() => setConfirmDelete(false)} disabled={busy !== null} className="flex-1 py-2 rounded-xl border border-white/10 text-sm text-secondary">Cancel</button>
                  <button onClick={doDelete} disabled={busy !== null} className="flex-1 py-2 rounded-xl bg-danger-solid text-danger-fg hover:opacity-90 text-sm font-medium disabled:opacity-50">
                    {busy === "delete" ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
