"use client";

// Phase 5G — ONE shared "Add to Schedule" surface for the whole app (Map/Explore
// and Activity both mount this exact component). A real place -> a persistent,
// backend-validated schedule event.
//
// Guarantees:
//   * No silent creation — the user sees a prefilled, EDITABLE draft and must
//     press Confirm.
//   * "Added" is shown ONLY after the backend confirms persistence (201/200).
//   * Duplicate-safe — one `client_token` is generated per open of the form, so a
//     double-click, a slow-network retry or a React Strict-Mode replay all reuse
//     it and the backend returns the first event (never a second one).
//   * Truthful failures — the backend's own message is shown (validation,
//     schedule conflict, save failure); nothing is invented.

import React, { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { X, CalendarPlus, CheckCircle2, MapPin } from "lucide-react";
import { apiFetch, ApiError, mutationOutcome, describeFailure } from "../lib/api";
import { newClientToken } from "../lib/clientToken";
import { usePendingCreate } from "../lib/usePendingCreate";
import type { EventPlaceRef } from "../lib/types";
import {
  prefillTitle,
  roundedWindow,
  draftError,
  buildCreateBody,
} from "../schedule/addToSchedule";
import { environmentForActivity, type Environment } from "../schedule/eventEnvironment";

export default function AddToScheduleSheet({
  place,
  activityLabel,
  activityId,
  providerTypes,
  onClose,
  onAdded,
}: {
  place: EventPlaceRef;
  activityLabel?: string | null;
  activityId?: string | null;
  providerTypes?: readonly string[] | null;
  onClose: () => void;
  onAdded?: (eventId: string) => void;
}) {
  // Seed the default one-hour window ONCE, when the form opens.
  const [win] = useState(() => roundedWindow(Date.now()));
  const [title, setTitle] = useState(() => prefillTitle({ placeName: place.name, activityLabel }));
  // Truthful, deterministic (no-AI) starting classification the user can adjust.
  const [environment, setEnvironment] = useState<Environment>(
    () => environmentForActivity(activityId, providerTypes),
  );
  const [startInput, setStartInput] = useState(win.startInput);
  const [endInput, setEndInput] = useState(win.endInput);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [savedId, setSavedId] = useState<string | null>(null);

  // ONE token for the lifetime of this open form (lazy init -> generated once,
  // not per click / per render). The sheet unmounts on close, so a re-open of
  // "Add to schedule" gets a fresh token.
  const [clientToken] = useState<string>(newClientToken);
  const pending = usePendingCreate("event", clientToken);
  const titleRef = useRef<HTMLInputElement>(null);
  const errId = useId();

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || savedId) return;
    const draft = { title, startInput, endInput };
    const problem = draftError(draft);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    try {
      pending.arm();
      const res = await apiFetch<{ status: string; event: { id: string }; idempotent?: boolean }>(
        "/api/events",
        {
          method: "POST",
          body: JSON.stringify(
            buildCreateBody({ title, startInput, endInput, ref: place, clientToken, environment }),
          ),
        },
      );
      // Success ONLY once the backend has confirmed the row.
      pending.resolve();
      setSavedId(res.event.id);
      onAdded?.(res.event.id);
    } catch (err) {
      const is409 = err instanceof ApiError && err.kind === "http" && err.status === 409;
      if (is409) {
        // This token already committed an event — keep the pending record so
        // reconciliation surfaces it.
        setError(
          "This was already added a moment ago, with different details. Check your schedule and refresh if it looks wrong.",
        );
      } else if (mutationOutcome(err) === "unknown") {
        setError(
          "We couldn't confirm whether this was added. Check your schedule — it may already be there — before trying again.",
        );
      } else {
        pending.resolve();
        setError(describeFailure(err, { verb: "add this to your schedule" }));
      }
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[110] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4"
      onClick={() => { if (!busy) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Add to schedule"
    >
      <div
        className="w-full max-w-md bg-surface border border-white/10 rounded-t-3xl sm:rounded-3xl p-6 space-y-4 max-h-[90vh] overflow-y-auto"
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <CalendarPlus size={18} className="text-emerald-400" /> Add to schedule
          </h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-white p-1"><X size={18} /></button>
        </div>

        <div className="flex items-start gap-2 text-sm text-secondary bg-white/[0.04] border border-white/10 rounded-xl px-3 py-2.5">
          <MapPin size={14} className="text-emerald-400 shrink-0 mt-0.5" />
          <span className="min-w-0">
            <span className="text-primary">{place.name}</span>
            {place.address && <span className="block text-[11px] text-muted truncate">{place.address}</span>}
          </span>
        </div>

        {savedId ? (
          <div className="space-y-4">
            <p className="flex items-center gap-2 text-sm text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-2.5">
              <CheckCircle2 size={16} className="shrink-0" /> Added to your schedule.
            </p>
            <div className="flex gap-2">
              <Link
                href="/schedule"
                className="flex-1 text-center bg-white text-black font-semibold py-3 rounded-xl"
              >
                View schedule
              </Link>
              <button onClick={onClose} className="px-4 bg-white/10 hover:bg-white/15 text-white py-3 rounded-xl text-sm font-semibold">
                Done
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            <label className="block">
              <span className="text-xs text-muted">Title</span>
              <input
                ref={titleRef}
                type="text"
                required
                maxLength={200}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="mt-1 w-full bg-field border border-field-border rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-ring"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-muted">Start</span>
                <input
                  type="datetime-local"
                  required
                  value={startInput}
                  onChange={(e) => setStartInput(e.target.value)}
                  className="mt-1 w-full bg-field border border-field-border rounded-xl px-2 py-2.5 text-sm text-white outline-none focus:border-ring"
                />
              </label>
              <label className="block">
                <span className="text-xs text-muted">End</span>
                <input
                  type="datetime-local"
                  required
                  value={endInput}
                  onChange={(e) => setEndInput(e.target.value)}
                  className="mt-1 w-full bg-field border border-field-border rounded-xl px-2 py-2.5 text-sm text-white outline-none focus:border-ring"
                />
              </label>
            </div>

            <label className="block">
              <span className="text-xs text-muted">Indoor or outdoor?</span>
              <select
                value={environment}
                onChange={(e) => setEnvironment(e.target.value as Environment)}
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

            <p className="text-[11px] text-muted">Times are in Malaysia time (GMT+8), like the rest of your schedule.</p>

            {pending.recovered && (
              <p className="text-sm text-amber-200 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2">
                An event you started earlier had already been saved. You may not need to add this one.
              </p>
            )}

            {error && (
              <p id={errId} role="alert" className="text-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy}
              aria-describedby={error ? errId : undefined}
              className="w-full bg-white text-black font-semibold py-3 rounded-xl disabled:opacity-50"
            >
              {busy ? "Adding…" : "Confirm"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
