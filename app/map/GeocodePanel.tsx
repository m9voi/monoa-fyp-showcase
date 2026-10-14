"use client";
// Phase 5D — "Change location" panel. Lets the user set an EXPLICIT search
// origin: either re-use the device location, or type an area name and pick a
// real geocoding result. Explicit "Search" button (not keystroke autocomplete)
// — simpler, cheaper, and within Nominatim's usage policy. Nothing is
// auto-selected: the user must tap a result.

import { useRef, useState } from "react";
import { X, Search, LocateFixed, RefreshCw, AlertTriangle, MapPin } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type { GeoResult, GeoSearchResponse, PlacesErrorKind } from "../lib/types";

interface Props {
  onPick: (r: GeoResult) => void;
  onUseCurrentLocation: () => void;
  onClose: () => void;
}

type GeoState =
  | { phase: "idle" }
  | { phase: "searching" }
  | { phase: "results"; results: GeoResult[]; attribution: string; provider: string }
  | { phase: "empty" }
  | { phase: "error"; kind: PlacesErrorKind };

const GEO_ERROR_COPY: Record<PlacesErrorKind, string> = {
  PROVIDER_NOT_CONFIGURED: "Location search isn't switched on for this server yet.",
  PROVIDER_UNAVAILABLE: "Location search is temporarily unavailable.",
  PROVIDER_TIMEOUT: "Location search took too long. Try again.",
  PROVIDER_RATE_LIMITED: "Location search is busy right now. Try again shortly.",
  INVALID_PROVIDER_RESPONSE: "Location search is temporarily unavailable.",
};

export default function GeocodePanel({ onPick, onUseCurrentLocation, onClose }: Props) {
  const [q, setQ] = useState("");
  const [geo, setGeo] = useState<GeoState>({ phase: "idle" });
  const seq = useRef(0);

  const runSearch = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const query = q.trim();
    if (query.length < 2) return;
    const my = ++seq.current;
    setGeo({ phase: "searching" });
    try {
      const data = await apiFetch<GeoSearchResponse>("/api/geocode/search", {
        method: "POST",
        kind: "provider",
        body: JSON.stringify({ query }),
      });
      if (my !== seq.current) return; // a newer search superseded this one
      if (data.status === "PROVIDER_ERROR") {
        setGeo({ phase: "error", kind: data.error ?? "PROVIDER_UNAVAILABLE" });
      } else if (data.status === "NO_RESULTS" || data.results.length === 0) {
        setGeo({ phase: "empty" });
      } else {
        setGeo({ phase: "results", results: data.results, attribution: data.attribution, provider: data.provider });
      }
    } catch (err) {
      if (my !== seq.current) return;
      let kind: PlacesErrorKind = "PROVIDER_UNAVAILABLE";
      if (err instanceof ApiError && err.status === 429) kind = "PROVIDER_RATE_LIMITED";
      else if (err instanceof ApiError && err.kind === "timeout") kind = "PROVIDER_TIMEOUT";
      else console.error("Geocode request failed:", err);
      setGeo({ phase: "error", kind });
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Change search location"
    >
      <div
        className="w-full max-w-md bg-surface border border-white/10 rounded-t-3xl sm:rounded-3xl p-5 pb-8 space-y-4 max-h-[85vh] overflow-y-auto scrollbar-hide"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-bold text-primary">Search location</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-white p-1"><X size={18} /></button>
        </div>

        <button
          onClick={onUseCurrentLocation}
          className="w-full flex items-center gap-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-2xl px-4 py-3 text-sm font-semibold text-white"
        >
          <LocateFixed size={16} className="text-blue-400" /> Use current location
        </button>

        <form onSubmit={runSearch} className="flex gap-2">
          <input
            type="text"
            inputMode="search"
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. Cyberjaya, KLCC, Putrajaya"
            aria-label="Area or place name"
            className="flex-1 bg-field border border-field-border rounded-2xl px-4 py-3 text-sm text-white placeholder:text-muted focus:outline-none focus:border-ring"
          />
          <button
            type="submit"
            disabled={q.trim().length < 2 || geo.phase === "searching"}
            className="px-4 bg-white text-black rounded-2xl text-sm font-bold flex items-center gap-1.5 disabled:opacity-40"
          >
            <Search size={15} /> Search
          </button>
        </form>

        <p aria-live="polite" className="sr-only">
          {geo.phase === "searching" ? "Searching for locations"
            : geo.phase === "results" ? `${geo.results.length} locations found`
            : geo.phase === "empty" ? "No locations found"
            : geo.phase === "error" ? GEO_ERROR_COPY[geo.kind] : ""}
        </p>

        {geo.phase === "searching" && (
          <div className="flex items-center justify-center gap-2 py-6 text-sm text-secondary">
            <RefreshCw size={16} className="text-emerald-400 animate-spin" /> Searching…
          </div>
        )}

        {geo.phase === "empty" && (
          <p className="text-sm text-secondary text-center py-6">No matching locations. Try a different name.</p>
        )}

        {geo.phase === "error" && (
          <div className="flex flex-col items-center gap-2 py-6 text-center">
            <AlertTriangle size={26} className="text-amber-400" />
            <p className="text-sm font-semibold max-w-xs">{GEO_ERROR_COPY[geo.kind]}</p>
            {geo.kind !== "PROVIDER_NOT_CONFIGURED" && (
              <button onClick={() => runSearch()} className="bg-white/10 hover:bg-white/15 text-white px-4 py-1.5 rounded-xl text-xs font-bold">Try again</button>
            )}
          </div>
        )}

        {geo.phase === "results" && (
          <div className="space-y-2">
            <ul className="space-y-2">
              {geo.results.map((r) => (
                <li key={r.id}>
                  <button
                    onClick={() => onPick(r)}
                    className="w-full text-left flex items-start gap-2.5 bg-surface hover:bg-white/[0.04] border border-white/10 rounded-2xl px-4 py-3"
                  >
                    <MapPin size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                    <span className="text-sm text-primary leading-snug">{r.label}</span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="text-[10px] text-muted text-center">
              {geo.provider === "google" ? "Google" : "OpenStreetMap"} · {geo.attribution}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
