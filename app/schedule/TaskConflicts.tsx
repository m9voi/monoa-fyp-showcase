"use client";

import React, { useRef, useState } from "react";
import { CalendarClock, CheckCircle2, RefreshCw, Loader2 } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type {
  TaskConflict, TaskCandidate,
  TaskAlternativesResponse, TaskDecisionResponse,
} from "../lib/types";
import {
  unresolvedTaskConflicts, taskConflictKey, occurrenceLabel, windowLabel, dateLabel,
  isOccurrenceScoped, type TaskUiState,
} from "./taskConflictView";

const BTN =
  "min-h-[44px] min-w-[44px] px-4 rounded-xl text-sm font-medium inline-flex items-center justify-center gap-2 " +
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring " +
  "disabled:opacity-50 disabled:cursor-not-allowed transition-colors";

type Local = {
  state: TaskUiState;
  proposalId?: string | null;
  candidates?: TaskCandidate[];
  chosen?: number;
  noAltMessage?: string;
};

export default function TaskConflicts({
  conflicts,
  onResolved,
}: {
  conflicts: TaskConflict[];
  onResolved: () => void;
}) {
  const [local, setLocal] = useState<Record<string, Local>>({});
  const [settled, setSettled] = useState<{ key: string; text: string }[]>([]);
  const busy = useRef<Set<string>>(new Set());

  const active = unresolvedTaskConflicts(conflicts).filter(
    (c) => !settled.some((s) => s.key === taskConflictKey(c)),
  );
  if (active.length === 0 && settled.length === 0) return null;

  const put = (key: string, patch: Partial<Local>) =>
    setLocal((p) => ({ ...p, [key]: { ...(p[key] ?? { state: "idle" }), ...patch } }));

  async function keep(c: TaskConflict) {
    const key = taskConflictKey(c);
    if (busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "keeping" });
    try {
      await apiFetch<TaskDecisionResponse>(
        `/api/schedule/task-conflicts/${c.fingerprint}/keep`,
        { method: "POST", kind: "crud" },
      );
      const occ = isOccurrenceScoped(c) ? occurrenceLabel(c.occurrence_date) : "";
      setSettled((p) => [...p.filter((s) => s.key !== key), {
        key,
        text: occ
          ? `Kept both for ${occ}. Only this occurrence is settled — other occurrences are unaffected.`
          : "Kept both as planned. This overlap won’t show as unresolved again.",
      }]);
      put(key, { state: "kept" });
      onResolved();
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function findTime(c: TaskConflict) {
    const key = taskConflictKey(c);
    if (busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "searching" });
    try {
      const r = await apiFetch<TaskAlternativesResponse>(
        `/api/schedule/task-conflicts/${c.fingerprint}/alternatives`,
        { method: "POST", kind: "crud" },
      );
      if (!r.proposal_id || r.candidates.length === 0) {
        put(key, { state: "no_alt", noAltMessage: r.message ?? "No free time is available for this task." });
      } else {
        put(key, { state: "candidates", proposalId: r.proposal_id, candidates: r.candidates, chosen: 0 });
      }
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function confirmMove(c: TaskConflict) {
    const key = taskConflictKey(c);
    const l = local[key];
    if (!l?.proposalId || l.chosen == null || busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "confirming" });
    const cand = l.candidates?.[l.chosen];
    try {
      await apiFetch<TaskDecisionResponse>(
        `/api/schedule/task-conflicts/alternatives/${l.proposalId}/confirm`,
        { method: "POST", kind: "crud", body: JSON.stringify({ candidate_index: l.chosen }) },
      );
      setSettled((p) => [...p.filter((s) => s.key !== key), {
        key,
        text: `Moved the work block for “${c.task.title ?? "the task"}” to ${cand?.summary ?? "a free time"}. The deadline is unchanged.`,
      }]);
      put(key, { state: "moved" });
      onResolved();
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function cancelMove(c: TaskConflict) {
    const key = taskConflictKey(c);
    const l = local[key];
    if (l?.proposalId) {
      try {
        await apiFetch(`/api/schedule/task-conflicts/alternatives/${l.proposalId}/cancel`,
          { method: "POST", kind: "crud" });
      } catch { /* best-effort */ }
    }
    put(key, { state: "idle", proposalId: null, candidates: undefined, chosen: undefined });
  }

  function dismiss(key: string) {
    setSettled((p) => p.filter((s) => s.key !== key));
    setLocal((p) => { const n = { ...p }; delete n[key]; return n; });
  }

  return (
    <section aria-label="Task time conflicts" className="space-y-2.5">
      {settled.map((s) => (
        <div key={s.key} className="flex items-start gap-2.5 text-xs bg-success/10 border border-success/20 rounded-xl px-3.5 py-3 text-success">
          <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
          <p className="flex-1">{s.text}</p>
          <button type="button" onClick={() => dismiss(s.key)}
            className="font-medium min-h-[44px] px-2 -my-2 text-success/90 hover:text-success">Done</button>
        </div>
      ))}

      {active.map((c) => {
        const key = taskConflictKey(c);
        const l: Local = local[key] ?? { state: "idle" };
        const disabled = l.state === "keeping" || l.state === "searching" || l.state === "confirming";
        const showCandidates = l.state === "candidates" || l.state === "confirming";
        const perOcc = isOccurrenceScoped(c);
        const occLabel = occurrenceLabel(c.occurrence_date);

        return (
          <div key={key} className="bg-surface border border-amber-500/25 rounded-2xl p-3.5 space-y-3">
            <div className="flex items-center gap-2 text-amber-400 text-[11px] font-bold uppercase tracking-widest">
              <CalendarClock size={13} /> Task runs into an event
            </div>

            {perOcc && occLabel && (
              <p className="inline-flex items-center gap-1.5 text-[11px] font-medium text-secondary bg-inset border border-line rounded-lg px-2 py-1">
                This occurrence · {occLabel}
              </p>
            )}

            <p className="text-sm text-primary leading-snug">{c.explanation}</p>

            <div className="text-xs text-muted space-y-0.5">
              <p><span className="text-secondary">{c.task.title}</span> · work block {windowLabel(c.task.start, c.task.end)}
                {c.task.due_date ? ` · due ${dateLabel(c.task.due_date)} (unchanged)` : ""}</p>
              <p><span className="text-secondary">{c.event.title}</span> · {windowLabel(c.event.start, c.event.end)}
                {c.event.recurring ? " · repeats" : ""}</p>
            </div>

            {l.state === "stale" && (
              <p className="text-[11px] text-warning">This conflict changed. Reload your plan.</p>
            )}
            {l.state === "error" && (
              <p className="text-[11px] text-danger">Couldn’t do that just now. Try again.</p>
            )}
            {l.state === "no_alt" && (
              <p className="text-xs text-secondary">{l.noAltMessage}</p>
            )}

            {showCandidates && l.candidates && (
              <fieldset className="space-y-1.5">
                <legend className="text-[11px] font-bold uppercase tracking-widest text-muted mb-1">
                  A free time for this task
                </legend>
                {l.candidates.map((cand, i) => (
                  <label key={cand.start}
                    className={`flex items-center gap-2.5 rounded-xl border px-3 py-2.5 cursor-pointer text-sm transition-colors focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ring ${
                      l.chosen === i ? "border-strong bg-inset text-primary" : "border-line bg-surface-2 text-secondary hover:bg-inset"
                    }`}>
                    <input type="radio" name={`te-${key}`} className="sr-only" disabled={disabled}
                      checked={l.chosen === i} onChange={() => put(key, { chosen: i })} />
                    <span>{cand.summary}</span>
                  </label>
                ))}
                <p className="text-[11px] text-muted pt-0.5">
                  Only the task work block moves — the {c.event.recurring ? "repeating event" : "event"}
                  {c.task.due_date ? ` and the ${dateLabel(c.task.due_date)} deadline` : " and the deadline"} stay where they are.
                  {c.task.due_date ? " Every option ends on or before the deadline." : ""}
                </p>
              </fieldset>
            )}

            <div className="flex flex-wrap gap-2 pt-0.5">
              {l.state === "stale" ? (
                <button type="button" onClick={onResolved} className={`${BTN} bg-strong text-strong-fg hover:opacity-90`}>
                  <RefreshCw size={15} /> Reload
                </button>
              ) : showCandidates ? (
                <>
                  <button type="button" disabled={disabled} onClick={() => confirmMove(c)}
                    className={`${BTN} bg-strong text-strong-fg hover:opacity-90`}>
                    {l.state === "confirming" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                    Confirm new time
                  </button>
                  <button type="button" disabled={disabled} onClick={() => cancelMove(c)}
                    className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                    Cancel
                  </button>
                </>
              ) : l.state === "no_alt" ? (
                <button type="button" disabled={disabled} onClick={() => keep(c)}
                  className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                  Keep both
                </button>
              ) : (
                <>
                  <button type="button" disabled={disabled} onClick={() => findTime(c)}
                    className={`${BTN} bg-strong text-strong-fg hover:opacity-90`}>
                    {l.state === "searching" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                    {l.state === "searching" ? "Finding a time…" : "Move this task"}
                  </button>
                  <button type="button" disabled={disabled} onClick={() => keep(c)}
                    className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                    {l.state === "keeping" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                    Keep both
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </section>
  );
}
