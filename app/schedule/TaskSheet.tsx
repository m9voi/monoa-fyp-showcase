"use client";

import React, { useState } from "react";
import { X, Trash2 } from "lucide-react";
import { apiFetch, ApiError, mutationOutcome, describeFailure } from "../lib/api";
import { newClientToken } from "../lib/clientToken";
import { usePendingCreate } from "../lib/usePendingCreate";
import { isoToLocalInput, localInputToIso } from "../lib/datetime";
import type { CoreTask } from "../lib/types";

type Mode = "create" | "edit";

export default function TaskSheet({
  mode,
  initial,
  defaultDateKey,
  onClose,
  onSaved,
}: {
  mode: Mode;
  initial?: CoreTask | null;
  defaultDateKey?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(initial?.title ?? "");
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [due, setDue] = useState(
    initial?.due_date ? isoToLocalInput(initial.due_date).slice(0, 10) : defaultDateKey ?? "",
  );
  const [planned, setPlanned] = useState(isoToLocalInput(initial?.planned_at));
  const [scheduledEnd, setScheduledEnd] = useState(isoToLocalInput(initial?.scheduled_end));
  const [reminder, setReminder] = useState(isoToLocalInput(initial?.reminder_at));
  const [busy, setBusy] = useState<null | "save" | "delete">(null);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Phase 5H-B3: one idempotency token per open form -> a retried submit cannot
  // create a second task.
  const [clientToken] = useState<string>(newClientToken);
  const pending = usePendingCreate("task", clientToken);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (!title.trim()) {
      setError("Give the task a title.");
      return;
    }
    setBusy("save");
    setError("");
    if (scheduledEnd && !planned) {
      setError("A planned end needs a planned start time.");
      setBusy(null);
      return;
    }
    if (planned && scheduledEnd && localInputToIso(scheduledEnd) <= localInputToIso(planned)) {
      setError("The end time must be after the start time.");
      setBusy(null);
      return;
    }
    const body: Record<string, unknown> = {
      title: title.trim(),
      notes: notes.trim() || null,
      due_date: due ? `${due}T23:59:00+08:00` : null,
      planned_at: planned ? localInputToIso(planned) : null,
      scheduled_end: planned && scheduledEnd ? localInputToIso(scheduledEnd) : null,
      reminder_at: reminder ? localInputToIso(reminder) : null,
    };
    try {
      if (mode === "create") {
        pending.arm();
        await apiFetch("/api/tasks", {
          method: "POST",
          body: JSON.stringify({ ...body, client_token: clientToken }),
        });
        pending.resolve();
      } else if (initial) {
        await apiFetch(`/api/tasks/${initial.id}`, { method: "PATCH", body: JSON.stringify(body) });
      }
      onSaved();
      onClose();
    } catch (err) {
      const is409 = err instanceof ApiError && err.kind === "http" && err.status === 409;
      if (mode === "create" && is409) {
        setError(
          "This was already saved a moment ago, with different details. Check your tasks and refresh if it looks wrong.",
        );
      } else if (mode === "create" && mutationOutcome(err) === "unknown") {
        setError(
          "We couldn't confirm whether this was saved. Check your tasks — it may already be there — before adding it again.",
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
      await apiFetch(`/api/tasks/${initial.id}`, { method: "DELETE" });
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
      aria-label={mode === "create" ? "Add task" : "Edit task"}
    >
      <div className="w-full max-w-md bg-surface border border-white/10 rounded-t-3xl sm:rounded-3xl p-6 space-y-4 max-h-[90vh] overflow-y-auto" onClick={(ev) => ev.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">{mode === "create" ? "Add task" : "Edit task"}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-white p-1"><X size={18} /></button>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <label className="block">
            <span className="text-xs text-muted">Title</span>
            <input
              type="text" required maxLength={200} value={title} autoFocus
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Finish presentation slides"
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
            <span className="text-xs text-muted">Due date (optional)</span>
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring" />
          </label>

          <label className="block">
            <span className="text-xs text-muted">Planned time (optional)</span>
            <input type="datetime-local" value={planned} onChange={(e) => setPlanned(e.target.value)}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring" />
          </label>

          {planned && (
            <label className="block">
              <span className="text-xs text-muted">Ends at (optional)</span>
              <input type="datetime-local" value={scheduledEnd} min={planned}
                onChange={(e) => setScheduledEnd(e.target.value)}
                className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring" />
              <span className="text-[11px] text-muted">Adds a work block to your plan. Plan flags overlaps with events; your deadline is separate.</span>
            </label>
          )}

          <label className="block">
            <span className="text-xs text-muted">Reminder (optional)</span>
            <input type="datetime-local" value={reminder} onChange={(e) => setReminder(e.target.value)}
              className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring" />
            <span className="text-[11px] text-muted">MONOA flags reminders as due at that time — no device push yet.</span>
          </label>

          {mode === "create" && pending.recovered && (
            <p className="text-sm text-amber-200 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2">
              A task you started earlier had already been saved. You may not need to add this one.
            </p>
          )}

          {error && (
            <p className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</p>
          )}

          <button type="submit" disabled={busy !== null} className="w-full bg-white text-black font-semibold py-3 rounded-xl disabled:opacity-50">
            {busy === "save" ? "Saving…" : mode === "create" ? "Add task" : "Save changes"}
          </button>
        </form>

        {mode === "edit" && initial && (
          <div className="pt-2 border-t border-white/5">
            {!confirmDelete ? (
              <button onClick={() => setConfirmDelete(true)} className="w-full flex items-center justify-center gap-2 text-red-400 hover:text-red-300 text-sm py-2">
                <Trash2 size={15} /> Delete task
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-secondary text-center">Delete this task? This cannot be undone.</p>
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
