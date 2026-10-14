"use client";

import React, { useRef, useState } from "react";
import {
  CloudLightning, CloudRain, Thermometer, Wind, Snowflake, CloudOff,
  CheckCircle2, RefreshCw, Loader2,
} from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type {
  WeatherConflict, WeatherCandidate,
  WeatherAlternativesResponse, WeatherDecisionResponse,
} from "../lib/types";
import {
  unresolvedWeather, weatherKey, hazardLabel, tierLabel,
  occurrenceLabel, isOccurrenceScoped,
  type WeatherUiState,
} from "./weatherConflictView";

const HAZARD_ICON: Record<string, React.ComponentType<{ size?: number; className?: string }>> = {
  thunderstorm: CloudLightning,
  heavy_rain: CloudRain,
  heavy_snow: Snowflake,
  heat: Thermometer,
  wind: Wind,
  cold: Snowflake,
};

const BTN =
  "min-h-[44px] min-w-[44px] px-4 rounded-xl text-sm font-medium inline-flex items-center justify-center gap-2 " +
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring " +
  "disabled:opacity-50 disabled:cursor-not-allowed transition-colors";

type Local = {
  state: WeatherUiState;
  proposalId?: string | null;
  candidates?: WeatherCandidate[];
  chosen?: number;
  noAltMessage?: string;
  ack?: string;
};

export default function WeatherConflicts({
  conflicts,
  providerUnavailable,
  attribution,
  onResolved,
}: {
  conflicts: WeatherConflict[];
  providerUnavailable?: boolean;
  attribution?: string;
  onResolved: () => void;
}) {
  const [local, setLocal] = useState<Record<string, Local>>({});
  const [settled, setSettled] = useState<{ key: string; text: string }[]>([]);
  const busy = useRef<Set<string>>(new Set());

  const active = unresolvedWeather(conflicts).filter(
    (c) => !settled.some((s) => s.key === weatherKey(c)),
  );

  if (active.length === 0 && settled.length === 0 && !providerUnavailable) return null;

  const put = (key: string, patch: Partial<Local>) =>
    setLocal((p) => ({ ...p, [key]: { ...(p[key] ?? { state: "idle" }), ...patch } }));

  async function keep(c: WeatherConflict) {
    const key = weatherKey(c);
    if (busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "keeping" });
    try {
      await apiFetch<WeatherDecisionResponse>(
        `/api/schedule/weather-conflicts/${c.fingerprint}/keep`,
        { method: "POST", kind: "provider" },
      );
      setSettled((p) => [...p.filter((s) => s.key !== key),
        { key, text: isOccurrenceScoped(c)
            ? `Kept ${occurrenceLabel(c.occurrence_date)} as planned. Only this occurrence is settled — other occurrences still get their own check.`
            : `Kept as planned. This weather note won’t show again unless conditions get worse.` }]);
      put(key, { state: "kept" });
      onResolved();
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function findTime(c: WeatherConflict) {
    const key = weatherKey(c);
    if (busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "searching" });
    try {
      const r = await apiFetch<WeatherAlternativesResponse>(
        `/api/schedule/weather-conflicts/${c.fingerprint}/alternatives`,
        { method: "POST", kind: "provider" },
      );
      if (!r.proposal_id || r.candidates.length === 0) {
        put(key, { state: "no_alt", noAltMessage: r.message ?? "No suitable alternative is available from the current forecast." });
      } else {
        put(key, { state: "candidates", proposalId: r.proposal_id, candidates: r.candidates, chosen: 0 });
      }
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function confirmMove(c: WeatherConflict) {
    const key = weatherKey(c);
    const l = local[key];
    if (!l?.proposalId || l.chosen == null || busy.current.has(key)) return;
    busy.current.add(key);
    put(key, { state: "confirming" });
    const cand = l.candidates?.[l.chosen];
    try {
      await apiFetch<WeatherDecisionResponse>(
        `/api/schedule/weather-conflicts/alternatives/${l.proposalId}/confirm`,
        { method: "POST", kind: "crud", body: JSON.stringify({ candidate_index: l.chosen }) },
      );
      setSettled((p) => [...p.filter((s) => s.key !== key),
        { key, text: isOccurrenceScoped(c)
            ? `Moved ${occurrenceLabel(c.occurrence_date)} to ${cand?.summary ?? "a lower-risk time"}. Only this occurrence changed — the repeat keeps its schedule.`
            : `Moved to ${cand?.summary ?? "a lower-risk time"}. Your plan is updated.` }]);
      put(key, { state: "moved" });
      onResolved();
    } catch (err) {
      put(key, { state: err instanceof ApiError && err.status === 409 ? "stale" : "error" });
    } finally {
      busy.current.delete(key);
    }
  }

  async function cancelMove(c: WeatherConflict) {
    const key = weatherKey(c);
    const l = local[key];
    if (l?.proposalId) {
      try {
        await apiFetch(`/api/schedule/weather-conflicts/alternatives/${l.proposalId}/cancel`,
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
    <section aria-label="Weather warnings" className="space-y-2.5">
      {providerUnavailable && active.length === 0 && settled.length === 0 && (
        <div className="flex items-center gap-2.5 text-xs bg-surface border border-line rounded-xl px-3.5 py-3 text-muted">
          <CloudOff size={15} className="shrink-0" />
          <p>Forecast is unavailable right now — weather warnings are paused.</p>
        </div>
      )}

      {settled.map((s) => (
        <div key={s.key} className="flex items-start gap-2.5 text-xs bg-success/10 border border-success/20 rounded-xl px-3.5 py-3 text-success">
          <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
          <p className="flex-1">{s.text}</p>
          <button type="button" onClick={() => dismiss(s.key)}
            className="font-medium min-h-[44px] px-2 -my-2 text-success/90 hover:text-success">Done</button>
        </div>
      ))}

      {active.map((c) => {
        const key = weatherKey(c);
        const l: Local = local[key] ?? { state: "idle" };
        const Icon = HAZARD_ICON[c.hazard_type] ?? CloudOff;
        const disabled = l.state === "keeping" || l.state === "searching" || l.state === "confirming";
        const showCandidates = l.state === "candidates" || l.state === "confirming";

        return (
          <div key={key} className="bg-surface border border-warning/30 rounded-2xl p-3.5 space-y-3">
            <div className="flex items-center gap-2 text-warning text-[11px] font-bold uppercase tracking-widest">
              <Icon size={13} /> Weather · {hazardLabel(c.hazard_type)} · {tierLabel(c.severity_tier)}
            </div>

            {isOccurrenceScoped(c) && occurrenceLabel(c.occurrence_date) && (
              <p className="inline-flex items-center gap-1.5 text-[11px] font-medium text-secondary bg-inset border border-line rounded-lg px-2 py-1">
                This occurrence · {occurrenceLabel(c.occurrence_date)}
              </p>
            )}

            <p className="text-sm text-primary leading-snug">{c.headline}</p>
            <p className="text-xs text-muted">
              {c.event.title || "Outdoor event"}
              {isOccurrenceScoped(c) ? " · repeats" : ""}
            </p>

            {l.state === "stale" && (
              <p className="text-[11px] text-warning">This warning is out of date. Reload your plan.</p>
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
                  Lower weather risk based on the current forecast
                </legend>
                {l.candidates.map((cand, i) => (
                  <label key={cand.start}
                    className={`flex items-center gap-2.5 rounded-xl border px-3 py-2.5 cursor-pointer text-sm transition-colors focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ring ${
                      l.chosen === i ? "border-strong bg-inset text-primary" : "border-line bg-surface-2 text-secondary hover:bg-inset"
                    }`}>
                    <input type="radio" name={`wx-${key}`} className="sr-only" disabled={disabled}
                      checked={l.chosen === i} onChange={() => put(key, { chosen: i })} />
                    <span>{cand.summary}</span>
                  </label>
                ))}
                {isOccurrenceScoped(c) && (
                  <p className="text-[11px] text-muted pt-0.5">
                    Confirming moves only {occurrenceLabel(c.occurrence_date)} — every other occurrence keeps its time.
                  </p>
                )}
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
                    {isOccurrenceScoped(c) ? "Confirm — this occurrence only" : "Confirm move"}
                  </button>
                  <button type="button" disabled={disabled} onClick={() => cancelMove(c)}
                    className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                    Cancel
                  </button>
                </>
              ) : l.state === "no_alt" ? (
                <button type="button" disabled={disabled} onClick={() => keep(c)}
                  className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                  Keep plan
                </button>
              ) : (
                <>
                  <button type="button" disabled={disabled} onClick={() => findTime(c)}
                    className={`${BTN} bg-strong text-strong-fg hover:opacity-90`}>
                    {l.state === "searching" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                    {l.state === "searching" ? "Checking forecast…" : "Find another time"}
                  </button>
                  <button type="button" disabled={disabled} onClick={() => keep(c)}
                    className={`${BTN} border border-line-strong text-primary hover:bg-surface-2`}>
                    {l.state === "keeping" && <Loader2 size={15} className="animate-spin motion-reduce:animate-[spin_1.6s_linear_infinite]" />}
                    Keep plan
                  </button>
                </>
              )}
            </div>

            {attribution && (
              <p className="text-[10px] text-faint">{attribution}</p>
            )}
          </div>
        );
      })}
    </section>
  );
}
