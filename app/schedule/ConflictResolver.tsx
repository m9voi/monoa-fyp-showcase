"use client";

import React, { useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, Loader2 } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type { ScheduleConflict, ScheduleConflictDecisionResponse } from "../lib/types";
import {
  unresolvedConflicts, conflictKey, spanLabel, shiftLabel,
  occurrenceLabel, isOccurrenceScoped,
  type ConflictUiState,
} from "./conflictView";

type Settled = { key: string; kind: "accepted" | "kept"; text: string };

const BTN =
  "min-h-[44px] min-w-[44px] px-4 rounded-xl text-sm font-medium inline-flex items-center justify-center gap-2 " +
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white/70 " +
  "disabled:opacity-50 disabled:cursor-not-allowed transition-colors";

export default function ConflictResolver({
  conflicts,
  onResolved,
}: {
  conflicts: ScheduleConflict[];
  onResolved: () => void;
}) {
  const [state, setState] = useState<Record<string, ConflictUiState>>({});
  const [settled, setSettled] = useState<Settled[]>([]);
  const busy = useRef<Set<string>>(new Set());

  const active = unresolvedConflicts(conflicts).filter(
    (c) => !settled.some((s) => s.key === conflictKey(c)),
  );

  if (active.length === 0 && settled.length === 0) return null;

  function set(key: string, s: ConflictUiState) {
    setState((prev) => ({ ...prev, [key]: s }));
  }

  async function run(
    c: ScheduleConflict,
    action: "accept" | "keep",
  ) {
    const key = conflictKey(c);
    if (!c.fingerprint || busy.current.has(key)) return;
    busy.current.add(key);
    set(key, action === "accept" ? "accepting" : "keeping");
    try {
      const res = await apiFetch<ScheduleConflictDecisionResponse>(
        `/api/schedule/conflicts/${c.fingerprint}/${action}`,
        { method: "POST", kind: "crud" },
      );
      const kind = res.decision === "accepted" ? "accepted" : "kept";
      const occ = res.scope === "occurrence" ? occurrenceLabel(res.occurrence_date) : "";
      const text =
        kind === "accepted"
          ? `Moved “${res.event?.title ?? "the event"}”${occ ? ` on ${occ}` : ""} to ${
              res.resolution ? spanLabel(res.resolution.start, res.resolution.end) : "its new time"
            }.${occ ? " Only this occurrence changed — the rest of the repeat is unchanged." : " Your plan is updated."}`
          : occ
            ? `Kept ${occ} as planned. Only this occurrence is settled — other occurrences are unaffected.`
            : "Kept both as planned. This overlap won’t show as unresolved again.";
      setSettled((prev) => [...prev.filter((s) => s.key !== key), { key, kind, text }]);
      set(key, kind);
      onResolved();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        set(key, "stale");
      } else if (err instanceof ApiError && err.status === 422) {
        // no safe reschedule — fall back to keep-only messaging
        set(key, "error");
      } else {
        set(key, "error");
      }
    } finally {
      busy.current.delete(key);
    }
  }

  function dismiss(key: string) {
    setSettled((prev) => prev.filter((s) => s.key !== key));
    setState((prev) => {
      const n = { ...prev };
      delete n[key];
      return n;
    });
  }

  return (
    <section aria-label="Scheduling overlaps" className="space-y-2.5">
      {settled.map((s) => (
        <div
          key={s.key}
          className="flex items-start gap-2.5 text-xs bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3.5 py-3 text-emerald-300"
        >
          <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
          <p className="flex-1">{s.text}</p>
          <button
            type="button"
            onClick={() => dismiss(s.key)}
            className="text-emerald-300/80 hover:text-emerald-200 font-medium min-h-[44px] px-2 -my-2"
          >
            Done
          </button>
        </div>
      ))}

      {active.map((c) => {
        const key = conflictKey(c);
        const st: ConflictUiState = state[key] ?? "idle";
        const proposal = c.proposal ?? null;
        const shift = shiftLabel(proposal);
        const disabled = st === "accepting" || st === "keeping";
        const perOcc = isOccurrenceScoped(c);
        const occLabel = occurrenceLabel(c.occurrence_date);

        return (
          <div
            key={key}
            className="bg-surface border border-amber-500/25 rounded-2xl p-3.5 space-y-3"
          >
            <div className="flex items-center gap-2 text-amber-400 text-[11px] font-bold uppercase tracking-widest">
              <AlertTriangle size={13} /> {perOcc ? "Repeating event overlap" : "Scheduling overlap"}
            </div>

            {perOcc && occLabel && (
              <p className="inline-flex items-center gap-1.5 text-[11px] font-medium text-secondary bg-inset border border-line rounded-lg px-2 py-1">
                This occurrence · {occLabel}
              </p>
            )}

            <p className="text-sm text-primary leading-snug">
              {c.explanation ??
                `“${c.a.title}” and “${c.b.title}” overlap.`}
            </p>

            <div className="text-xs text-muted space-y-0.5">
              <p>
                <span className="text-secondary">{c.a.title}</span> ·{" "}
                {spanLabel(c.a.start, c.a.end)}
              </p>
              <p>
                <span className="text-secondary">{c.b.title}</span> ·{" "}
                {spanLabel(c.b.start, c.b.end)}
              </p>
            </div>

            {c.decidable === false ? (
              <p className="text-xs text-muted">
                Open an event to change its time.
              </p>
            ) : shift ? (
              <div className="bg-white/5 border border-white/10 rounded-xl px-3 py-2.5">
                <p className="text-xs text-primary">{shift}</p>
                <p className="text-[11px] text-muted mt-0.5">
                  {perOcc
                    ? "Its length stays the same. Only this occurrence moves — the repeat keeps its schedule."
                    : "Its length stays the same. Nothing else moves."}
                </p>
              </div>
            ) : (
              <p className="text-xs text-secondary">
                No automatic reschedule is available — the times can’t shift
                without creating another clash. You can keep the current plan.
              </p>
            )}

            {st === "stale" && (
              <p className="text-[11px] text-amber-300">
                This overlap changed. Reload to see the latest.
              </p>
            )}
            {st === "error" && (
              <p className="text-[11px] text-red-300">
                Couldn’t save that just now. Try again.
              </p>
            )}

            {c.decidable !== false && (
              <div className="flex flex-wrap gap-2 pt-0.5">
                {st === "stale" ? (
                  <button
                    type="button"
                    onClick={onResolved}
                    className={`${BTN} bg-white text-black hover:bg-white/90`}
                  >
                    <RefreshCw size={15} /> Reload
                  </button>
                ) : (
                  <>
                    {shift && (
                      <button
                        type="button"
                        disabled={disabled}
                        onClick={() => run(c, "accept")}
                        className={`${BTN} bg-white text-black hover:bg-white/90`}
                      >
                        {st === "accepting" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                        {st === "accepting"
                          ? "Applying…"
                          : perOcc ? "Move this occurrence" : "Accept suggestion"}
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() => run(c, "keep")}
                      className={`${BTN} border border-white/20 text-white hover:bg-white/5`}
                    >
                      {st === "keeping" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                      {st === "keeping"
                        ? "Saving…"
                        : perOcc ? "Keep this occurrence" : "Keep current plan"}
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}
