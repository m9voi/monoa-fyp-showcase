"use client";
// Phase 5E — CoreOS Activity: "I know what I want to do."
//
// An activity INTENT (not location discovery — that is Map/Explore). Choose an
// activity, CoreOS resolves the current/manual origin, the deterministic
// Activity Engine (backend /api/activities/search) returns ranked, truthfully
// classified real candidates, each with an evidence-grounded reason. Nothing
// here is fabricated: no invented conditions, no hardcoded city, no fake places.
//
// Phase 07D (Figma 250:130 / 443:2-212): Figma's Activity Home and its A2-A9
// flow depict a materially different product — 5 broad intent categories
// (Move/Play/Go out/Watch/Focus) instead of the real 10 sport-specific
// categories, a proactive "For you now" recommendation feed (weather + time
// awareness), a "Your activity" usage-history/stats view, and named sample
// place results (Taman Tasik, MMU Stadium…). None of that exists in the real
// product (no recommendation engine, no usage-history aggregation) — adding
// it would be a business-logic change, not a visual one, so it is
// deliberately NOT implemented here (see the batch report's out-of-scope
// section). What IS adopted is Figma's card/typography/color LANGUAGE (the
// same eyebrow+tag+card system already used by Mo/Plan) applied to every
// REAL state this page already has: the real 10-category picker, real
// permission/loading/error states (styled after A3/A4/A8/A9, which are
// template-like and 100% real-data-safe), and real result cards.

import React, { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import {
  Activity as ActivityIcon,
  MapPin, MapPinOff, Navigation, RefreshCw, AlertTriangle, ShieldCheck,
  Star, LocateFixed, CheckCircle2, HelpCircle, CalendarPlus, Map as MapIcon,
} from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type {
  ActivitySearchResponse, ActivityCandidate, ActivityRelevanceClass, PlacesErrorKind, GeoResult,
} from "../lib/types";
import { useDeviceOrigin } from "../lib/useDeviceOrigin";
import { locationRecoveryAction } from "../lib/location";
import { safeExternalHref } from "../lib/safeUrl";
import { originIsVeryApproximate } from "../map/searchOrigin";
import { expandedRadiusNote } from "./searchStatus";
import { directionsUrl, distanceLabel } from "../map/mapState";
import { toPlaceRef, activityFocusUrl } from "../schedule/placeLink";
import AddToScheduleSheet from "../components/AddToScheduleSheet";
import GeocodePanel from "../map/GeocodePanel";
import WeatherLine from "./WeatherLine";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

export default function ActivityPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-app" />}>
      <ActivityInner />
    </Suspense>
  );
}

const ACTIVITIES: Array<{ id: string; label: string }> = [
  { id: "futsal", label: "Futsal" },
  { id: "football", label: "Football" },
  { id: "basketball", label: "Basketball" },
  { id: "volleyball", label: "Volleyball" },
  { id: "badminton", label: "Badminton" },
  { id: "swimming", label: "Swimming" },
  { id: "running", label: "Running" },
  { id: "cycling", label: "Cycling" },
  { id: "tennis", label: "Tennis" },
  { id: "golf", label: "Golf" },
];

// Figma's 5-color intent-chip palette (Move/Play/Go out/Watch/Focus), reused
// cyclically as pure decoration across the real 10 categories — never a
// re-categorisation of them.
const CHIP_TINT_CLASSES = ["bg-chip-move", "bg-chip-play", "bg-chip-goout", "bg-chip-watch", "bg-chip-focus"];

type SearchState =
  | { phase: "idle" }
  | { phase: "searching" }
  | { phase: "results"; res: ActivitySearchResponse }
  | { phase: "empty"; radiusM: number }
  | { phase: "error"; kind: PlacesErrorKind }
  | { phase: "neterror" };

const ERR: Record<PlacesErrorKind, string> = {
  PROVIDER_NOT_CONFIGURED: "Activity search isn't switched on for this server yet.",
  PROVIDER_UNAVAILABLE: "The place data source is temporarily unavailable.",
  PROVIDER_TIMEOUT: "The search took too long. Try again.",
  PROVIDER_RATE_LIMITED: "Activity search is busy right now. Try again shortly.",
  INVALID_PROVIDER_RESPONSE: "The place data source returned something unexpected.",
};

const CLASS_META: Record<ActivityRelevanceClass, { label: string; cls: string; icon: React.ReactNode }> = {
  strong: { label: "Confirmed", cls: "text-success bg-success/10 border-success/25", icon: <CheckCircle2 size={12} /> },
  likely: { label: "Likely", cls: "text-info bg-info/10 border-info/25", icon: <ActivityIcon size={12} /> },
  possible: { label: "Not confirmed", cls: "text-warning bg-warning/10 border-warning/25", icon: <HelpCircle size={12} /> },
};

function ActivityInner() {
  const [activity, setActivity] = useState<string | null>(null);
  const [search, setSearch] = useState<SearchState>({ phase: "idle" });
  const [geoOpen, setGeoOpen] = useState(false);

  const { origin, deviceLoc, acquireDevice, setManualOrigin, reqSeqRef, mountedRef } = useDeviceOrigin();

  const veryApprox = originIsVeryApproximate(origin);
  const activityLabel = useMemo(
    () => ACTIVITIES.find((a) => a.id === activity)?.label ?? activity ?? "",
    [activity],
  );

  const runSearch = async (act: string, o: NonNullable<typeof origin>) => {
    const my = ++reqSeqRef.current;
    setSearch({ phase: "searching" });
    try {
      const res = await apiFetch<ActivitySearchResponse>("/api/activities/search", {
        method: "POST",
        kind: "provider",
        body: JSON.stringify({
          activity_id: act,
          origin: {
            latitude: o.latitude,
            longitude: o.longitude,
            source: o.source,
            accuracy_m: o.source === "device" ? o.accuracyMeters : null,
          },
        }),
      });
      if (my !== reqSeqRef.current || !mountedRef.current) return;
      if (res.status === "PROVIDER_ERROR") setSearch({ phase: "error", kind: res.error ?? "PROVIDER_UNAVAILABLE" });
      else if (res.status === "NO_RESULTS" || res.candidates.length === 0)
        setSearch({ phase: "empty", radiusM: res.radius_m });
      else setSearch({ phase: "results", res });
    } catch (err) {
      if (my !== reqSeqRef.current || !mountedRef.current) return;
      if (err instanceof ApiError && err.status === 401) { setSearch({ phase: "neterror" }); return; }
      // CoreOS-side 429 — surface it as a truthful "slow down", not a network error.
      if (err instanceof ApiError && err.status === 429) {
        setSearch({ phase: "error", kind: "PROVIDER_RATE_LIMITED" });
        return;
      }
      // A deadline timeout is "the search took too long", NOT "you're offline"
      // and NOT a location problem.
      if (err instanceof ApiError && err.kind === "timeout") {
        setSearch({ phase: "error", kind: "PROVIDER_TIMEOUT" });
        return;
      }
      console.error("Activity search failed:", err);
      setSearch({ phase: "neterror" });
    }
  };

  const pickActivity = (id: string) => {
    setActivity(id);
    if (origin) void runSearch(id, origin);
    else acquireDevice({ onOrigin: (o) => void runSearch(id, o) });
  };
  const retryDevice = (opts?: { highAccuracy?: boolean }) =>
    acquireDevice({ ...opts, onOrigin: (o) => { if (activity) void runSearch(activity, o); } });
  const useCurrentLocation = () => {
    setGeoOpen(false);
    acquireDevice({ onOrigin: (o) => { if (activity) void runSearch(activity, o); } });
  };
  const pickGeoResult = (r: GeoResult) => {
    const o = setManualOrigin(r);          // bumps reqSeqRef -> invalidates stale results
    setGeoOpen(false);
    if (activity) void runSearch(activity, o);
  };
  const changeActivity = () => {
    reqSeqRef.current++;
    setActivity(null);
    setSearch({ phase: "idle" });
  };
  const retrySearch = () => { if (activity && origin) void runSearch(activity, origin); };

  const live =
    deviceLoc.phase === "requesting" ? "Getting your location"
    : deviceLoc.phase === "failed" ? deviceLoc.message
    : search.phase === "searching" ? `Finding ${activityLabel.toLowerCase()} options`
    : search.phase === "results" ? `${search.res.candidates.length} options found`
    : search.phase === "empty" ? "No relevant places found"
    : search.phase === "error" ? ERR[search.kind]
    : search.phase === "neterror" ? "Couldn't reach the server"
    : "";

  const showPicker = !activity && deviceLoc.phase !== "requesting" && deviceLoc.phase !== "failed";

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header>
            <h1
              className="text-[26px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Activity
            </h1>
            <p className="mt-1 text-[13px] text-muted" style={{ fontFamily: BODY_FONT }}>
              What are you up for?
            </p>
          </header>

          <p aria-live="polite" className="sr-only">{live}</p>

          {/* ---- origin chip ---- */}
          {origin && (
            <div className="flex items-center justify-between gap-2 bg-surface border border-line rounded-[16px] px-3 py-2.5">
              <p className="text-[11px] text-secondary flex items-center gap-1.5 min-w-0" style={{ fontFamily: BODY_FONT }}>
                {origin.source === "manual"
                  ? <MapPin size={12} className="text-warning shrink-0" />
                  : <LocateFixed size={12} className={veryApprox ? "text-warning shrink-0" : "text-info shrink-0"} />}
                <span className="truncate">
                  {origin.source === "manual"
                    ? <>Around <span className="text-primary font-medium">{origin.label}</span></>
                    : veryApprox ? "Your location (very approximate)" : "Your location"}
                  {activity && <span className="text-muted"> · {activityLabel}</span>}
                </span>
              </p>
              <button onClick={() => setGeoOpen(true)} className="shrink-0 text-[10px] font-bold text-muted uppercase tracking-wide hover:text-primary">Change</button>
            </div>
          )}

          {/* ---- activity picker (real 10 categories; Figma's 5-color chip
              palette reused cyclically as decoration only) ---- */}
          {showPicker && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2.5">
                {ACTIVITIES.map((a, i) => (
                  <button
                    key={a.id}
                    onClick={() => pickActivity(a.id)}
                    className={`h-11 rounded-[19px] px-4 inline-flex items-center justify-center font-semibold text-[11px] text-primary transition-opacity hover:opacity-80 ${CHIP_TINT_CLASSES[i % CHIP_TINT_CLASSES.length]}`}
                    style={{ fontFamily: BODY_FONT }}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
              {!origin && (
                <button onClick={() => setGeoOpen(true)} className="w-full py-2.5 text-[11px] font-semibold text-muted hover:text-secondary flex items-center justify-center gap-1.5" style={{ fontFamily: BODY_FONT }}>
                  <MapPin size={12} /> Or search a specific area first
                </button>
              )}
            </div>
          )}

          {/* ---- acquiring device location ---- */}
          {deviceLoc.phase === "requesting" && (
            <div className="flex flex-col items-center justify-center py-16 space-y-4">
              <RefreshCw size={28} className="text-muted animate-spin" />
              <p className="text-sm text-secondary" style={{ fontFamily: BODY_FONT }}>Getting your location…</p>
              <button onClick={() => setGeoOpen(true)} className="text-[11px] font-semibold text-muted hover:text-primary underline underline-offset-2" style={{ fontFamily: BODY_FONT }}>
                Search a location instead
              </button>
            </div>
          )}

          {/* ---- device location failure (Figma A9 card language: eyebrow +
              tag + real message + primary/secondary actions) ---- */}
          {deviceLoc.phase === "failed" && (
            <div className="rounded-[22px] border border-line bg-surface p-[22px] text-center">
              <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>LOCATION</p>
              <MapPinOff size={30} className="text-warning mx-auto mt-3" />
              <p className="mt-3 text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{deviceLoc.message}</p>
              <div className="mt-4 flex flex-wrap gap-2 justify-center">
                {locationRecoveryAction(deviceLoc.status) === "settings" ? (
                  <Link href="/settings" className="bg-[#1b2130] hover:opacity-90 text-[#fafaf5] px-5 py-2.5 rounded-[16px] text-xs font-bold min-h-[44px] inline-flex items-center focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">Open Settings</Link>
                ) : locationRecoveryAction(deviceLoc.status) === "retry" ? (
                  <button onClick={() => retryDevice()} className="bg-[#1b2130] hover:opacity-90 text-[#fafaf5] px-5 py-2.5 rounded-[16px] text-xs font-bold">Try again</button>
                ) : (
                  <button onClick={() => window.location.reload()} className="bg-[#1b2130] hover:opacity-90 text-[#fafaf5] px-5 py-2.5 rounded-[16px] text-xs font-bold">Reload page</button>
                )}
                <button onClick={() => setGeoOpen(true)} className="border border-line bg-surface hover:bg-surface-2 text-primary px-5 py-2.5 rounded-[16px] text-xs font-bold">Search a location</button>
                <button onClick={changeActivity} className="border border-line bg-surface hover:bg-surface-2 text-secondary px-5 py-2.5 rounded-[16px] text-xs font-bold">Back</button>
              </div>
            </div>
          )}

          {/* ---- results area ---- */}
          {activity && origin && deviceLoc.phase === "idle" && (
            <div className="space-y-4">
              {veryApprox && (
                <div className="flex items-start gap-2 text-[11px] text-warning bg-warning/10 border border-warning/20 rounded-[16px] px-3 py-2.5">
                  <AlertTriangle size={14} className="text-warning shrink-0 mt-0.5" />
                  <div className="space-y-1.5">
                    <p style={{ fontFamily: BODY_FONT }}>Your device location is only a rough estimate, so these results may not really be near you. Distances aren&apos;t shown.</p>
                    <div className="flex gap-3">
                      <button onClick={() => retryDevice({ highAccuracy: true })} className="font-bold underline underline-offset-2 hover:text-primary">Try location again</button>
                      <button onClick={() => setGeoOpen(true)} className="font-bold underline underline-offset-2 hover:text-primary">Search a location</button>
                    </div>
                  </div>
                </div>
              )}

              {/* Phase 5F: minimal contextual weather line at the search origin.
                  Read-only; does NOT influence the ranking below. Re-keyed on
                  the origin so it refetches when the user changes location. */}
              <WeatherLine key={`${origin.latitude},${origin.longitude}`} origin={origin} />

              {search.phase === "searching" && (
                <div className="rounded-[22px] border border-line bg-surface p-[22px]">
                  <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>SEARCHING NEARBY</p>
                  <div className="mt-3 flex items-center gap-2.5">
                    <RefreshCw size={18} className="text-muted animate-spin shrink-0" />
                    <p className="text-[13px] text-secondary" style={{ fontFamily: BODY_FONT }}>Finding {activityLabel.toLowerCase()} options…</p>
                  </div>
                  <div className="mt-4 space-y-2.5" aria-hidden="true">
                    <div className="h-3 w-[70%] rounded-full bg-inset" />
                    <div className="h-3 w-[85%] rounded-full bg-inset" />
                    <div className="h-3 w-[45%] rounded-full bg-inset" />
                  </div>
                </div>
              )}

              {search.phase === "empty" && (
                <div className="rounded-[22px] border border-line bg-surface p-[22px] text-center">
                  <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>SEARCH</p>
                  <span className="mt-2 inline-flex h-[26px] items-center rounded-[13px] bg-success/10 px-3 text-[11px] font-semibold text-success">NO RESULTS</span>
                  <p className="mt-2.5 text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
                    No {activityLabel.toLowerCase()} places found{search.radiusM >= 20_000 ? " within 20 km" : " nearby"}.
                  </p>
                  <p className="mt-1.5 text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>
                    Try a different area or another activity.
                  </p>
                  <button onClick={() => setGeoOpen(true)} className="mt-3 min-h-[44px] text-[13px] font-bold text-secondary underline underline-offset-2">Search a different area</button>
                </div>
              )}

              {(search.phase === "error" || search.phase === "neterror") && (
                <div className="rounded-[22px] border border-line bg-surface p-[22px] text-center">
                  <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>WHAT HAPPENED</p>
                  <span className="mt-2 inline-flex h-[26px] items-center rounded-[13px] bg-danger/10 px-3 text-[11px] font-semibold text-danger">SEARCH FAILED</span>
                  <p className="mt-2.5 text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
                    {search.phase === "error" ? ERR[search.kind] : "Couldn't reach the server. Check your connection and try again."}
                  </p>
                  {(search.phase === "neterror" || search.kind !== "PROVIDER_NOT_CONFIGURED") && (
                    <button onClick={retrySearch} className="mt-4 bg-[#1b2130] hover:opacity-90 text-[#fafaf5] px-5 py-2.5 rounded-[16px] text-xs font-bold">Try again</button>
                  )}
                </div>
              )}

              {search.phase === "results" && (
                <div className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h2 className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>
                      {activityLabel.toUpperCase()} · {search.res.candidates.length} OPTION{search.res.candidates.length === 1 ? "" : "S"}
                    </h2>
                    {expandedRadiusNote(search.res.radius_m) && (
                      <span className="shrink-0 text-[10px] font-medium text-muted" style={{ fontFamily: BODY_FONT }}>
                        {expandedRadiusNote(search.res.radius_m)}
                      </span>
                    )}
                  </div>
                  <ul className="space-y-2.5">
                    {search.res.candidates.map((c) => (
                      <li key={c.place_id}>
                        <ActivityResultCard c={c} distanceConfident={search.res.distance_confident} activityLabel={activityLabel} activityId={activity} />
                      </li>
                    ))}
                  </ul>
                  <p className="text-[10px] text-muted text-center pt-1" style={{ fontFamily: BODY_FONT }}>
                    Places &amp; evidence from {search.res.provider === "google" ? "Google" : "OpenStreetMap"} · {search.res.attribution}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        {geoOpen && (
          <GeocodePanel onPick={pickGeoResult} onUseCurrentLocation={useCurrentLocation} onClose={() => setGeoOpen(false)} />
        )}
      </div>
    </div>
  );
}

function ActivityResultCard({ c, distanceConfident, activityLabel, activityId }: { c: ActivityCandidate; distanceConfident: boolean; activityLabel: string; activityId: string | null }) {
  const [open, setOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const rel = c.activity_relevance;
  const meta = CLASS_META[rel.class];
  // Internal Map "View on map" — only when this real result has the stable
  // identity + valid coordinates to focus the Map on it. Otherwise the control
  // is simply absent (never a dead link, never a fabricated location).
  const mapHref = activityFocusUrl(c, activityId);
  return (
    <div className={`bg-surface border rounded-[20px] p-4 shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] transition-colors ${open ? "border-line-strong" : "border-line"}`}>
      <button onClick={() => setOpen((v) => !v)} aria-expanded={open} className="w-full text-left">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="text-[15px] font-bold text-primary truncate" style={{ fontFamily: BODY_FONT }}>{c.name}</h3>
            <p className="text-xs text-muted mt-0.5" style={{ fontFamily: BODY_FONT }}>
              {distanceLabel(c.distance_m, distanceConfident)}
              {distanceConfident && " · straight line"}
            </p>
          </div>
          <span className={`shrink-0 inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full border ${meta.cls}`}>
            {meta.icon} {meta.label}
          </span>
        </div>
        <p className="text-[11px] text-secondary mt-2 leading-relaxed" style={{ fontFamily: BODY_FONT }}>{rel.reason}</p>
      </button>

      {open && (() => {
        // Only render a metadata row when the provider actually supplied that
        // fact. Absent fields are simply omitted rather than shown as a
        // placeholder, and never given an invented fallback. Raw evidence
        // tier/signals stay in the payload for internal use, not shown here.
        const hoursText =
          c.open_now === true ? "open now"
          : c.open_now === false ? "closed now"
          : c.evidence.hours.has_hours ? "hours listed" : null;
        const rows: Array<[string, string]> = [];
        if (c.rating != null) rows.push(["Rating", `${c.rating.toFixed(1)} / 5${c.rating_count != null ? ` · ${c.rating_count} reviews` : ""}`]);
        if (hoursText) rows.push(["Opening hours", hoursText]);
        if (c.phone) rows.push(["Phone", c.phone]);
        return (
        <div className="mt-3 pt-3 border-t border-line space-y-2.5">
          {/* Figma A6/A7's provider-identity tag, bound to the real provider. */}
          <span className="inline-flex h-[22px] items-center rounded-[11px] bg-success/10 px-2.5 text-[10px] font-semibold text-success">
            {c.provider === "google" ? "GOOGLE" : "OPENSTREETMAP"}
          </span>
          {c.address && <p className="text-xs text-secondary" style={{ fontFamily: BODY_FONT }}>{c.address}</p>}
          {rows.length > 0 && (
            <dl className="text-xs text-secondary space-y-1">
              {rows.map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3" style={{ fontFamily: BODY_FONT }}><dt className="text-muted">{k}</dt><dd className="text-right">{v}</dd></div>
              ))}
            </dl>
          )}
          <div className="flex gap-2 pt-1">
            <a href={directionsUrl(c)} target="_blank" rel="noreferrer"
              aria-label={`Open directions to ${c.name} in an external map`}
              className="flex-1 bg-[#1b2130] hover:opacity-90 text-[#fafaf5] py-2.5 rounded-[16px] text-sm font-bold flex items-center justify-center gap-2">
              <Navigation size={16} /> Directions
            </a>
            {safeExternalHref(c.website) && (
              <a href={safeExternalHref(c.website)!} target="_blank" rel="noreferrer" className="px-4 border border-line bg-surface hover:bg-surface-2 text-primary py-2.5 rounded-[16px] text-sm font-semibold flex items-center">Website</a>
            )}
          </div>
          {mapHref && (
            <Link
              href={mapHref}
              aria-label={`View ${c.name} on the map`}
              className="w-full min-h-[44px] border border-line bg-surface hover:bg-surface-2 text-primary py-2.5 rounded-[16px] text-sm font-bold flex items-center justify-center gap-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
              <MapIcon size={16} className="text-success" /> View on map
            </Link>
          )}
          <button
            onClick={() => setAddOpen(true)}
            className="w-full min-h-[44px] border border-line bg-surface hover:bg-surface-2 text-primary py-2.5 rounded-[16px] text-sm font-bold flex items-center justify-center gap-2">
            <CalendarPlus size={16} className="text-success" /> Add to Schedule
          </button>
          {addOpen && (
            <AddToScheduleSheet
              place={toPlaceRef(c)}
              activityLabel={activityLabel}
              activityId={activityId}
              providerTypes={c.provider_types}
              onClose={() => setAddOpen(false)}
            />
          )}
          <p className="text-[10px] text-muted flex items-center gap-1" style={{ fontFamily: BODY_FONT }}>
            <ShieldCheck size={11} className="text-success" /> Identity from {c.provider === "google" ? "Google" : "OpenStreetMap"}
            {c.rating != null && <span className="ml-1 inline-flex items-center gap-0.5"><Star size={9} className="fill-warning text-warning" />{c.rating.toFixed(1)}</span>}
          </p>
          {rel.class === "possible" && (
            <p className="text-[10px] text-warning flex items-center gap-1" style={{ fontFamily: BODY_FONT }}>
              <HelpCircle size={11} /> {c.provider === "google" ? "Google" : "OpenStreetMap"} doesn&apos;t confirm this venue supports {rel.is_public_geography ? "the activity" : "it"} — check before you go.
            </p>
          )}
        </div>
        );
      })()}
    </div>
  );
}
