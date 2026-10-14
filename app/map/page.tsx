"use client";
import React, { Suspense, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Compass, Activity, MapPin, MapPinOff, Star, Navigation, ShieldCheck, RefreshCw, AlertTriangle, Waves, Trophy, Flame, ChevronLeft, LocateFixed, CalendarPlus } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import type { PlacesSearchResponse, PlacesErrorKind, CorePlace, GeoResult, EventPlaceRef, PlaceResolveResponse } from "../lib/types";
import { useDeviceOrigin } from "../lib/useDeviceOrigin";
import { locationRecoveryAction } from "../lib/location";
import { safeExternalHref } from "../lib/safeUrl";
import MapCanvas from "./MapCanvas";
import GeocodePanel from "./GeocodePanel";
import { reconcileSelection, directionsUrl, distanceLabel } from "./mapState";
import { type SearchOrigin, originLabel, originDistanceReliable, originIsVeryApproximate } from "./searchOrigin";
import { type LatLng, shouldOfferAreaSearch, expandedRadiusNote } from "./searchThisArea";
import { parseFocusRef, parseFocusSource, resolveRequestBody, resolveStatusNote, placeDirectionsUrl, toPlaceRef, type FocusSource } from "../schedule/placeLink";
import AddToScheduleSheet from "../components/AddToScheduleSheet";

export default function MapPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-app" />}>
      <MapPageInner />
    </Suspense>
  );
}

// Truthful place-search state (Phase 5C) — a provider failure is NOT "no results".
type SearchState =
  | { phase: "idle" }
  | { phase: "searching" }
  | { phase: "results"; places: CorePlace[]; attribution: string; provider: string; radiusM: number }
  | { phase: "empty"; radiusM: number }
  | { phase: "error"; kind: PlacesErrorKind };

const ERROR_COPY: Record<PlacesErrorKind, string> = {
  PROVIDER_NOT_CONFIGURED: "Place search isn't switched on for this server yet.",
  PROVIDER_UNAVAILABLE: "Nearby search is temporarily unavailable.",
  PROVIDER_TIMEOUT: "Nearby search took too long. Try again.",
  PROVIDER_RATE_LIMITED: "Nearby search is busy right now. Try again shortly.",
  INVALID_PROVIDER_RESPONSE: "Nearby search is temporarily unavailable.",
};

const ACTIVITIES: Array<{ key: string; label: string; icon: React.ReactNode; cls: string }> = [
  { key: "futsal", label: "Futsal", icon: <Flame size={22} className="text-emerald-400" />, cls: "bg-emerald-500/10 border-emerald-500/20 hover:bg-emerald-500/20" },
  { key: "badminton", label: "Badminton", icon: <Trophy size={22} className="text-blue-400" />, cls: "bg-blue-500/10 border-blue-500/20 hover:bg-blue-500/20" },
  { key: "swimming", label: "Swimming", icon: <Waves size={22} className="text-cyan-400" />, cls: "bg-cyan-500/10 border-cyan-500/20 hover:bg-cyan-500/20" },
  { key: "running", label: "Running", icon: <Activity size={22} className="text-pink-400" />, cls: "bg-pink-500/10 border-pink-500/20 hover:bg-pink-500/20" },
  { key: "basketball", label: "Basketball", icon: <MapPin size={20} className="text-orange-400" />, cls: "bg-white/5 border-white/10 hover:bg-white/10" },
  { key: "cycling", label: "Cycling", icon: <Compass size={20} className="text-slate-300" />, cls: "bg-white/5 border-white/10 hover:bg-white/10" },
  { key: "football", label: "Field Football", icon: <Trophy size={20} className="text-amber-400" />, cls: "bg-white/5 border-white/10 hover:bg-white/10" },
];

function providerLabel(p: string): string {
  return p === "google" ? "Google" : "OpenStreetMap";
}
function activityLabel(key: string | null): string {
  return key ? (ACTIVITIES.find((a) => a.key === key)?.label ?? key) : "";
}

function MapPageInner() {
  const params = useSearchParams();
  // Phase 5G: Schedule -> Map "View place" — an identity-preserving handoff
  // (`?focus_provider/&focus_id/&focus_lat/&focus_lng...`, never the user origin).
  const focusRef = useMemo(() => parseFocusRef(params), [params]);
  const focusSource = useMemo(() => parseFocusSource(params) ?? "schedule", [params]);
  const scheduleQuery = params.get("from") === "schedule" ? (params.get("q") || "").trim() : "";
  const scheduleTitle = (params.get("title") || "").trim();

  const [activity, setActivity] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [search, setSearch] = useState<SearchState>({ phase: "idle" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [geoOpen, setGeoOpen] = useState(false);
  // Phase 5G: the place currently being added to the schedule (shared sheet).
  const [addSched, setAddSched] = useState<EventPlaceRef | null>(null);
  // B10 "Search this area": the settled Leaflet centre (transient — never
  // persisted or logged) and the centre of the last COMPLETED search. The
  // control appears only once these diverge by a meaningful distance.
  const [mapCenter, setMapCenter] = useState<LatLng | null>(null);
  const [lastSearchCenter, setLastSearchCenter] = useState<LatLng | null>(null);
  const [areaBusy, setAreaBusy] = useState(false);

  // The ONE shared device-location acquisition hook (Phase 5D semantics:
  // watchdog + page-level fail-safe + Strict-Mode-safe mountedRef flag + request
  // token + abort-bounded validate). `reqSeqRef` / `mountedRef` are shared with the
  // place-search flow below so a manual origin change invalidates it too.
  const { origin, deviceLoc, acquireDevice, setManualOrigin, reqSeqRef, mountedRef } = useDeviceOrigin();

  const places = useMemo(
    () => (search.phase === "results" ? search.places : []),
    [search],
  );
  const selectedPlace = useMemo(
    () => places.find((p) => p.place_id === selectedId) ?? null,
    [places, selectedId],
  );

  useEffect(() => {
    if (!selectedId) return;
    document.getElementById(`place-${selectedId}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  // ------- place search: ALWAYS from the one explicit origin -------
  // B10: the Map/Explore flow searches in ADAPTIVE mode — the backend runs the
  // staged 8 -> 12 -> 20 km ladder with a strict per-rung within-radius gate
  // (parity with Activity). No fixed radius is sent.
  const runSearch = async (act: string, o: SearchOrigin) => {
    const my = ++reqSeqRef.current;
    setSelectedId(null);
    setSearch({ phase: "searching" });
    try {
      const data = await apiFetch<PlacesSearchResponse>("/api/places/search", {
        method: "POST",
        kind: "provider",
        body: JSON.stringify({ activity: act, latitude: o.latitude, longitude: o.longitude, adaptive_radius: true }),
      });
      if (my !== reqSeqRef.current || !mountedRef.current) return;
      if (data.status === "PROVIDER_ERROR") {
        setSearch({ phase: "error", kind: data.error ?? "PROVIDER_UNAVAILABLE" });
      } else if (data.status === "NO_RESULTS" || data.places.length === 0) {
        setSearch({ phase: "empty", radiusM: data.radius_m });
        setLastSearchCenter({ latitude: o.latitude, longitude: o.longitude });
        setMapCenter(null);
      } else {
        setSearch({ phase: "results", places: data.places, attribution: data.attribution, provider: data.provider, radiusM: data.radius_m });
        setSelectedId((prev) => reconcileSelection(prev, data.places));
        setLastSearchCenter({ latitude: o.latitude, longitude: o.longitude });
        setMapCenter(null);
      }
    } catch (err) {
      if (my !== reqSeqRef.current || !mountedRef.current) return;
      // A CoreOS-side 429 (too many requests) is not a provider outage; a
      // deadline timeout is "took too long", not an outage either.
      let kind: PlacesErrorKind = "PROVIDER_UNAVAILABLE";
      if (err instanceof ApiError && err.status === 429) kind = "PROVIDER_RATE_LIMITED";
      else if (err instanceof ApiError && err.kind === "timeout") kind = "PROVIDER_TIMEOUT";
      else console.error("Place search request failed:", err);
      setSearch({ phase: "error", kind });
    }
  };

  // ------- entry points -------
  const pickActivity = (act: string) => {
    setActivity(act);
    setSelectedId(null);
    if (origin) void runSearch(act, origin);
    else acquireDevice({ onOrigin: (o) => void runSearch(act, o) });
  };
  const retryDevice = (opts?: { highAccuracy?: boolean }) =>
    acquireDevice({ ...opts, onOrigin: (o) => { if (activity) void runSearch(activity, o); } });
  const useCurrentLocation = () => {
    setGeoOpen(false);
    acquireDevice({ onOrigin: (o) => { if (activity) void runSearch(activity, o); } });
  };
  const pickGeoResult = (r: GeoResult) => {
    const o = setManualOrigin(r);   // bumps reqSeqRef -> invalidates any in-flight GPS
    setGeoOpen(false);
    setSelectedId(null);
    if (activity) void runSearch(activity, o);
  };
  const changeActivity = () => {
    reqSeqRef.current++;               // invalidate any pending acquire/search
    setActivity(null);
    setSearch({ phase: "idle" });
    setSelectedId(null);
    // origin is kept — the user is still in the same place, just changing sport
  };
  const retrySearch = () => {
    if (activity && origin) void runSearch(activity, origin);
  };

  // B10 "Search this area": search from the CURRENT visible map centre. This is
  // a user-picked area, NOT the device location — it is set as a manual origin
  // labelled "This area" (no geocode call, no geolocation permission, nothing
  // persisted or logged) and searched in adaptive mode.
  const onAreaSearch = () => {
    if (!activity || !mapCenter || areaBusy) return;
    setAreaBusy(true);
    const o = setManualOrigin({ latitude: mapCenter.latitude, longitude: mapCenter.longitude, label: "This area" });
    setSelectedId(null);
    void runSearch(activity, o).finally(() => {
      if (mountedRef.current) setAreaBusy(false);
    });
  };
  // Visible while an area search is running (loading state) OR once the user has
  // panned the map meaningfully away from the last completed search centre.
  const showAreaSearchButton =
    areaBusy ||
    (search.phase === "results" && shouldOfferAreaSearch(lastSearchCenter, mapCenter));

  const reliable = origin ? originDistanceReliable(origin) : true;
  const veryApprox = originIsVeryApproximate(origin);

  const liveMessage =
    deviceLoc.phase === "requesting" ? "Getting your location"
    : deviceLoc.phase === "failed" ? deviceLoc.message
    : search.phase === "searching" ? "Searching nearby places"
    : search.phase === "results" ? `${search.places.length} places found`
    : search.phase === "empty" ? "No matching places found nearby"
    : search.phase === "error" ? ERROR_COPY[search.kind]
    : "";

  const showActivityPicker = !activity && deviceLoc.phase !== "requesting" && deviceLoc.phase !== "failed";
  const showResultsArea = !!activity && !!origin && deviceLoc.phase === "idle";

  // Phase 5G: a Schedule -> Map "View place" handoff takes over the whole screen
  // with a dedicated, origin-free focus view.
  if (focusRef) return <FocusPlaceView key={focusRef.place_id} focusRef={focusRef} source={focusSource} />;

  return (
    <div className="min-h-screen bg-app text-slate-100 font-sans flex justify-center">
      <div className="w-full max-w-md bg-app min-h-screen relative overflow-hidden flex flex-col border-x border-white/5">
        <div className="p-5 pb-28 flex flex-col h-full overflow-y-auto scrollbar-hide space-y-5">
          <header className="pt-4">
            <h1 className="monoaPageTitle text-primary">Explore</h1>
            <p className="text-sm font-medium text-muted mt-1 tracking-wide">Real places near you</p>
          </header>

          <p aria-live="polite" className="sr-only">{liveMessage}</p>

          {scheduleQuery && (
            <div className="bg-surface border border-white/10 rounded-2xl p-4 space-y-2">
              <p className="text-[10px] font-bold uppercase tracking-widest text-muted">From your schedule</p>
              {scheduleTitle && <p className="text-sm text-primary">{scheduleTitle}</p>}
              <p className="text-sm text-white flex items-center gap-1.5"><MapPin size={14} className="text-emerald-400" /> {scheduleQuery}</p>
              <p className="text-[11px] text-muted">This is the text from your schedule — not yet a matched place.</p>
              <a href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(scheduleQuery)}`} target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-2 bg-strong text-strong-fg hover:opacity-90 text-sm font-medium px-4 py-2 rounded-xl">
                <Navigation size={15} /> Open in Maps
              </a>
            </div>
          )}

          {/* ---- search-origin chip ---- */}
          {origin && (
            <div className="flex items-center justify-between gap-2 bg-white/[0.04] border border-white/10 rounded-xl px-3 py-2">
              <p className="text-[11px] text-secondary flex items-center gap-1.5 min-w-0">
                {origin.source === "manual"
                  ? <MapPin size={12} className="text-amber-400 shrink-0" />
                  : <LocateFixed size={12} className={veryApprox ? "text-amber-400 shrink-0" : "text-blue-400 shrink-0"} />}
                <span className="truncate">
                  {origin.source === "manual"
                    ? <>Searching around <span className="text-primary">{origin.label}</span></>
                    : veryApprox ? "Your location (very approximate)" : originLabel(origin)}
                  {activity && <span className="text-muted"> · {activityLabel(activity)}</span>}
                </span>
              </p>
              <button onClick={() => setGeoOpen(true)} className="shrink-0 text-[10px] font-bold text-muted uppercase tracking-widest hover:text-primary">Change</button>
            </div>
          )}

          {/* ---- activity picker ---- */}
          {showActivityPicker && (
            <div className="space-y-3">
              <h2 className="text-xs font-bold text-secondary uppercase tracking-widest pl-1">Choose an activity</h2>
              <div className="grid grid-cols-2 gap-3">
                {ACTIVITIES.slice(0, 4).map((a) => (
                  <button key={a.key} onClick={() => pickActivity(a.key)}
                    className={`border p-4 rounded-2xl flex flex-col items-center gap-2 transition-colors ${a.cls}`}>
                    {a.icon}<span className="font-semibold text-sm">{a.label}</span>
                  </button>
                ))}
              </div>
              {!showAll ? (
                <button onClick={() => setShowAll(true)} className="w-full py-3 text-xs font-bold text-muted border border-white/10 rounded-xl">More activities</button>
              ) : (
                <div className="grid grid-cols-2 gap-3">
                  {ACTIVITIES.slice(4).map((a) => (
                    <button key={a.key} onClick={() => pickActivity(a.key)}
                      className={`border p-4 rounded-2xl flex flex-col items-center gap-2 transition-colors ${a.cls} ${a.key === "football" ? "col-span-2" : ""}`}>
                      {a.icon}<span className="font-semibold text-sm">{a.label}</span>
                    </button>
                  ))}
                </div>
              )}
              {!origin && (
                <button onClick={() => setGeoOpen(true)} className="w-full py-2.5 text-[11px] font-semibold text-muted hover:text-secondary flex items-center justify-center gap-1.5">
                  <MapPin size={12} /> Or search a specific area
                </button>
              )}
            </div>
          )}

          {/* ---- acquiring device location ---- */}
          {deviceLoc.phase === "requesting" && (
            <div className="flex flex-col items-center justify-center py-16 space-y-4">
              <RefreshCw size={30} className="text-emerald-400 animate-spin" />
              <p className="text-sm text-secondary">Getting your location…</p>
              {/* Manual origin stays reachable WHILE the device fix is pending —
                  picking one invalidates this request (pickGeoResult bumps the
                  token and clears deviceLoc). */}
              <button
                onClick={() => setGeoOpen(true)}
                className="text-[11px] font-semibold text-muted hover:text-primary underline underline-offset-2"
              >
                Search a location instead
              </button>
            </div>
          )}

          {/* ---- device location failure — universal escape hatch ---- */}
          {deviceLoc.phase === "failed" && (
            <div className="flex flex-col items-center justify-center py-12 space-y-4 text-center">
              <MapPinOff size={36} className="text-amber-400" />
              <p className="text-sm font-semibold text-primary max-w-xs">{deviceLoc.message}</p>
              <div className="flex flex-wrap gap-2 justify-center">
                {locationRecoveryAction(deviceLoc.status) === "settings" ? (
                  <Link href="/settings" className="bg-white/10 hover:bg-white/15 text-white px-5 py-2.5 rounded-xl text-xs font-bold min-h-[44px] inline-flex items-center focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white/70">Open Settings</Link>
                ) : locationRecoveryAction(deviceLoc.status) === "retry" ? (
                  <button onClick={() => retryDevice()} className="bg-white/10 hover:bg-white/15 text-white px-5 py-2.5 rounded-xl text-xs font-bold">Try again</button>
                ) : (
                  <button onClick={() => window.location.reload()} className="bg-white/10 hover:bg-white/15 text-white px-5 py-2.5 rounded-xl text-xs font-bold">Reload page</button>
                )}
                <button onClick={() => setGeoOpen(true)} className="bg-strong text-strong-fg hover:opacity-90 px-5 py-2.5 rounded-xl text-xs font-bold">Search a location</button>
                <button onClick={changeActivity} className="bg-white/5 hover:bg-white/10 text-secondary px-5 py-2.5 rounded-xl text-xs font-bold">Back</button>
              </div>
            </div>
          )}

          {/* ---- origin set + activity chosen: map + results ---- */}
          {showResultsArea && origin && (
            <div className="space-y-4">
              {veryApprox && (
                <div className="flex items-start gap-2 text-[11px] text-amber-200/90 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2.5">
                  <AlertTriangle size={14} className="text-amber-400 shrink-0 mt-0.5" />
                  <div className="space-y-1.5">
                    <p>Your device location is only a rough estimate, so these places may not really be near you. Distances aren&apos;t shown.</p>
                    <div className="flex gap-3">
                      <button onClick={() => retryDevice({ highAccuracy: true })} className="font-bold underline underline-offset-2 hover:text-white">Try location again</button>
                      <button onClick={() => setGeoOpen(true)} className="font-bold underline underline-offset-2 hover:text-white">Search a location</button>
                    </div>
                  </div>
                </div>
              )}

              <div className="relative">
                <MapCanvas
                  origin={{
                    latitude: origin.latitude,
                    longitude: origin.longitude,
                    accuracyMeters: origin.source === "device" ? origin.accuracyMeters : null,
                    kind: origin.source,
                  }}
                  places={places}
                  selectedId={selectedId}
                  onSelectPlace={setSelectedId}
                  onCenterSettled={(c) => setMapCenter(c)}
                  className="h-60 w-full rounded-2xl overflow-hidden border border-white/10 relative z-0"
                />
                {showAreaSearchButton && (
                  <button
                    type="button"
                    onClick={onAreaSearch}
                    disabled={areaBusy}
                    aria-busy={areaBusy}
                    aria-label="Search this area"
                    className="absolute top-3 left-1/2 -translate-x-1/2 z-[500] min-h-[44px] min-w-[44px] px-4 inline-flex items-center justify-center gap-2 rounded-full bg-surface border border-white/15 text-primary text-xs font-bold shadow-lg hover:bg-white/10 disabled:opacity-70 disabled:pointer-events-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white/70 motion-safe:transition-colors"
                  >
                    {areaBusy ? (
                      <>
                        <RefreshCw size={14} className="text-emerald-400 animate-spin motion-reduce:animate-none" />
                        Searching…
                      </>
                    ) : (
                      <>
                        <Compass size={14} className="text-emerald-400" />
                        Search this area
                      </>
                    )}
                  </button>
                )}
              </div>

              {search.phase === "searching" && (
                <div className="flex items-center justify-center gap-2 py-6 text-sm text-secondary">
                  <RefreshCw size={16} className="text-emerald-400 animate-spin" /> Searching nearby places…
                </div>
              )}

              {search.phase === "empty" && (
                <div className="flex flex-col items-center gap-2 py-8 text-center">
                  <MapPin size={30} className="text-muted" />
                  <p className="text-sm font-semibold">
                    No matching places found{search.radiusM >= 20_000 ? " within 20 km" : " nearby"}.
                  </p>
                  <p className="text-[11px] text-muted max-w-[15rem]">Try a different activity or a different area.</p>
                </div>
              )}

              {search.phase === "error" && (
                <div className="flex flex-col items-center gap-3 py-8 text-center">
                  <AlertTriangle size={30} className="text-amber-400" />
                  <p className="text-sm font-semibold max-w-xs">{ERROR_COPY[search.kind]}</p>
                  {search.kind !== "PROVIDER_NOT_CONFIGURED" && (
                    <button onClick={retrySearch} className="bg-white/10 hover:bg-white/15 text-white px-5 py-2 rounded-xl text-xs font-bold">Try again</button>
                  )}
                </div>
              )}

              {/* ---- selected place detail (map stays mountedRef above) ---- */}
              {search.phase === "results" && selectedPlace && (
                <div className="bg-surface border border-white/10 rounded-2xl p-4 space-y-3">
                  <button onClick={() => setSelectedId(null)} className="text-[11px] text-muted hover:text-white flex items-center gap-1">
                    <ChevronLeft size={13} /> Back to results
                  </button>
                  <div>
                    <h3 className="text-lg font-bold text-primary">{selectedPlace.name}</h3>
                    <p className="text-xs text-muted mt-0.5">
                      {distanceLabel(selectedPlace.distance_m, reliable)}
                      {reliable && " · straight line"}
                      {selectedPlace.activity_match === "broad" && ` · may offer ${selectedPlace.activity}`}
                    </p>
                  </div>
                  {selectedPlace.address && <p className="text-xs text-secondary">{selectedPlace.address}</p>}

                  <div className="flex flex-wrap gap-1.5 text-[11px]">
                    {selectedPlace.rating != null && (
                      <span className="inline-flex items-center gap-1 text-amber-300 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-full">
                        <Star size={11} className="fill-amber-300" /> {selectedPlace.rating.toFixed(1)}
                        {selectedPlace.rating_count != null && <span className="text-muted">({selectedPlace.rating_count})</span>}
                      </span>
                    )}
                    {selectedPlace.evidence.hours.open_now === true && (
                      <span className="text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 px-2 py-0.5 rounded-full">Open now</span>
                    )}
                    {selectedPlace.evidence.hours.open_now === false && (
                      <span className="text-muted bg-white/5 border border-white/10 px-2 py-0.5 rounded-full">Closed now</span>
                    )}
                    {selectedPlace.business_status === "CLOSED_TEMPORARILY" && (
                      <span className="text-amber-300 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-full">Temporarily closed</span>
                    )}
                    {selectedPlace.business_status === "CLOSED_PERMANENTLY" && (
                      <span className="text-red-300 bg-red-500/10 border border-red-500/20 px-2 py-0.5 rounded-full">Permanently closed</span>
                    )}
                  </div>

                  <dl className="text-xs text-secondary space-y-1">
                    <div className="flex justify-between gap-3"><dt className="text-muted">Rating</dt><dd>{selectedPlace.rating != null ? `${selectedPlace.rating.toFixed(1)} / 5${selectedPlace.rating_count != null ? ` · ${selectedPlace.rating_count} reviews` : ""}` : "not available"}</dd></div>
                    <div className="flex justify-between gap-3"><dt className="text-muted">Opening hours</dt><dd>{selectedPlace.evidence.hours.has_hours ? (selectedPlace.open_now === true ? "open now" : selectedPlace.open_now === false ? "closed now" : "listed on Google") : "not available"}</dd></div>
                    <div className="flex justify-between gap-3"><dt className="text-muted">Phone</dt><dd>{selectedPlace.phone ?? "not available"}</dd></div>
                  </dl>

                  <div className="flex gap-2 pt-1">
                    <a href={directionsUrl(selectedPlace)} target="_blank" rel="noreferrer"
                      aria-label={`Open directions to ${selectedPlace.name} in an external map`}
                      className="flex-1 bg-strong text-strong-fg hover:opacity-90 py-2.5 rounded-xl text-sm font-bold flex items-center justify-center gap-2">
                      <Navigation size={16} /> Directions
                    </a>
                    {safeExternalHref(selectedPlace.website) && (
                      <a href={safeExternalHref(selectedPlace.website)!} target="_blank" rel="noreferrer"
                        className="px-4 bg-white/10 hover:bg-white/15 text-white py-2.5 rounded-xl text-sm font-semibold flex items-center">Website</a>
                    )}
                  </div>
                  <button
                    onClick={() => setAddSched(toPlaceRef(selectedPlace))}
                    className="w-full bg-white/10 hover:bg-white/15 text-white py-2.5 rounded-xl text-sm font-bold flex items-center justify-center gap-2">
                    <CalendarPlus size={16} className="text-emerald-400" /> Add to Schedule
                  </button>
                  <p className="text-[10px] text-muted flex items-center gap-1">
                    <ShieldCheck size={11} className="text-emerald-400" /> Identity from {providerLabel(selectedPlace.provider)}
                  </p>
                </div>
              )}

              {/* ---- results list (always usable, independent of the map) ---- */}
              {search.phase === "results" && !selectedPlace && (
                <div className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h2 className="text-xs font-bold text-secondary uppercase tracking-widest">
                      {origin.source === "manual" ? "Places in this area" : "Nearby places"}
                    </h2>
                    {expandedRadiusNote(search.radiusM) && (
                      <span className="shrink-0 text-[10px] font-medium text-muted">{expandedRadiusNote(search.radiusM)}</span>
                    )}
                  </div>
                  <ul className="space-y-2.5">
                    {search.places.map((place) => (
                      <li key={place.place_id}>
                        <button
                          id={`place-${place.place_id}`}
                          onClick={() => setSelectedId(place.place_id)}
                          aria-pressed={selectedId === place.place_id}
                          className={`w-full text-left bg-surface border rounded-2xl p-4 transition-colors ${
                            selectedId === place.place_id ? "border-emerald-500/60 bg-emerald-500/5" : "border-white/10 hover:bg-white/[0.03]"
                          }`}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0">
                              <h3 className="text-[15px] font-bold text-primary truncate">{place.name}</h3>
                              <p className="text-xs text-muted mt-0.5">
                                {distanceLabel(place.distance_m, reliable)}
                                {place.activity_match === "broad" && ` · may offer ${place.activity}`}
                              </p>
                            </div>
                            {place.rating != null && (
                              <span className="shrink-0 inline-flex items-center gap-1 text-[11px] text-amber-300">
                                <Star size={11} className="fill-amber-300" /> {place.rating.toFixed(1)}
                              </span>
                            )}
                          </div>
                          {place.address && <p className="text-[11px] text-muted mt-1.5 truncate">{place.address}</p>}
                          <div className="flex flex-wrap gap-1.5 mt-2 text-[10px]">
                            {place.evidence.hours.open_now === true && <span className="text-emerald-300 bg-emerald-500/10 px-1.5 py-0.5 rounded">Open now</span>}
                            {place.evidence.hours.open_now === false && <span className="text-muted bg-white/5 px-1.5 py-0.5 rounded">Closed now</span>}
                            {place.business_status === "CLOSED_PERMANENTLY" && <span className="text-red-300 bg-red-500/10 px-1.5 py-0.5 rounded">Permanently closed</span>}
                            {place.evidence.hours.source === null && place.rating == null && (
                              <span className="text-muted bg-white/5 px-1.5 py-0.5 rounded">Hours &amp; ratings unavailable</span>
                            )}
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                  <p className="text-[10px] text-muted text-center pt-1">
                    Places from {providerLabel(search.provider)} · {search.attribution}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        {geoOpen && (
          <GeocodePanel
            onPick={pickGeoResult}
            onUseCurrentLocation={useCurrentLocation}
            onClose={() => setGeoOpen(false)}
          />
        )}

        {addSched && (
          <AddToScheduleSheet
            place={addSched}
            activityLabel={activity ? activityLabel(activity) : null}
            activityId={activity}
            providerTypes={selectedPlace?.provider_types}
            onClose={() => setAddSched(null)}
          />
        )}

      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Phase 5G — Schedule -> Map "View place" focus view.
// Resolves the STORED place reference to fresh details (or a truthful snapshot),
// centres the map on it, and offers Directions + Add to Schedule. It never shows
// or requests the user's current location.
// --------------------------------------------------------------------------- //
type FocusState =
  | { phase: "loading" }
  | {
      phase: "ready";
      place: PlaceResolveResponse["place"];
      live: boolean;
      note: string | null;
      provider: string;
      attribution: string;
    };

function FocusPlaceView({ focusRef, source }: { focusRef: EventPlaceRef; source: FocusSource }) {
  const [st, setSt] = useState<FocusState>({ phase: "loading" });
  const [addOpen, setAddOpen] = useState(false);
  const backHref = source === "activity" ? "/activity" : "/schedule";
  const backLabel = source === "activity" ? "Back to activity" : "Back to schedule";
  const contextLabel = source === "activity" ? "From Activity" : "From your schedule";

  useEffect(() => {
    // One-shot: this component is re-keyed on focusRef.place_id, so it always
    // mounts fresh in the "loading" state and resolves exactly once.
    let alive = true;
    void (async () => {
      try {
        const r = await apiFetch<PlaceResolveResponse>("/api/places/resolve", {
          method: "POST",
          kind: "provider",
          body: JSON.stringify(resolveRequestBody(focusRef)),
        });
        if (!alive) return;
        setSt({
          phase: "ready",
          place: r.place,
          live: r.status === "LIVE",
          note: resolveStatusNote(r),
          provider: r.provider,
          attribution: r.attribution,
        });
      } catch {
        if (!alive) return;
        // The resolve request itself failed (server unreachable / auth). Fall
        // back to the snapshot carried in the URL — truthfully labelled.
        setSt({
          phase: "ready",
          place: { ...focusRef, maps_url: placeDirectionsUrl(focusRef) },
          live: false,
          note: "Couldn't reach the server just now — showing the details saved with your event.",
          provider: focusRef.provider,
          attribution: "",
        });
      }
    })();
    return () => {
      alive = false;
    };
  }, [focusRef]);

  const providerName = (p: string) => (p === "google" ? "Google" : "OpenStreetMap");
  const place = st.phase === "ready" ? st.place : null;
  const lat = place?.latitude ?? focusRef.latitude;
  const lng = place?.longitude ?? focusRef.longitude;
  const name = place?.name || focusRef.name;
  const address = place?.address ?? focusRef.address;
  const linkRef: EventPlaceRef = { ...focusRef, name, address };
  const canvasPlace = {
    ...(place ?? {}),
    place_id: focusRef.place_id,
    provider: focusRef.provider,
    provider_place_id: focusRef.provider_place_id,
    name,
    latitude: lat,
    longitude: lng,
  } as unknown as CorePlace;

  return (
    <div className="min-h-screen bg-app text-slate-100 font-sans flex justify-center">
      <div className="w-full max-w-md bg-app min-h-screen relative overflow-hidden flex flex-col border-x border-white/5">
        <div className="p-5 pb-10 flex flex-col h-full overflow-y-auto scrollbar-hide space-y-4">
          <header className="pt-4 flex items-center gap-2">
            <Link href={backHref} aria-label={backLabel} className="text-muted hover:text-white p-1 -ml-1 min-h-[44px] min-w-[44px] flex items-center justify-center focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white/70 rounded-lg">
              <ChevronLeft size={20} />
            </Link>
            <div>
              <p className="text-[10px] font-bold uppercase tracking-widest text-muted">{contextLabel}</p>
              <h1 className="text-xl font-semibold tracking-tight text-primary">{name}</h1>
            </div>
          </header>

          {st.phase === "loading" && (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-secondary">
              <RefreshCw size={16} className="text-emerald-400 animate-spin" /> Loading place…
            </div>
          )}

          {st.phase === "ready" && (
            <>
              <MapCanvas
                origin={null}
                places={[canvasPlace]}
                selectedId={focusRef.place_id}
                onSelectPlace={() => {}}
                className="h-60 w-full rounded-2xl overflow-hidden border border-white/10 relative z-0"
              />

              <div className="bg-surface border border-white/10 rounded-2xl p-4 space-y-3">
                {address && <p className="text-xs text-secondary">{address}</p>}

                {st.live ? (
                  <div className="flex flex-wrap gap-1.5 text-[11px]">
                    {typeof place?.rating === "number" && (
                      <span className="inline-flex items-center gap-1 text-amber-300 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-full">
                        <Star size={11} className="fill-amber-300" /> {place.rating.toFixed(1)}
                        {typeof place?.rating_count === "number" && <span className="text-muted">({place.rating_count})</span>}
                      </span>
                    )}
                    {place?.open_now === true && (
                      <span className="text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 px-2 py-0.5 rounded-full">Open now</span>
                    )}
                    {place?.open_now === false && (
                      <span className="text-muted bg-white/5 border border-white/10 px-2 py-0.5 rounded-full">Closed now</span>
                    )}
                    {place?.business_status === "CLOSED_TEMPORARILY" && (
                      <span className="text-amber-300 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-full">Temporarily closed</span>
                    )}
                    {place?.business_status === "CLOSED_PERMANENTLY" && (
                      <span className="text-red-300 bg-red-500/10 border border-red-500/20 px-2 py-0.5 rounded-full">Permanently closed</span>
                    )}
                  </div>
                ) : null}

                {st.note && (
                  <p className="text-[11px] text-amber-200/80 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
                    {st.note}
                  </p>
                )}

                <div className="flex gap-2 pt-1">
                  <a
                    href={placeDirectionsUrl(linkRef)}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`Open directions to ${name} in an external map`}
                    className="flex-1 bg-strong text-strong-fg hover:opacity-90 py-2.5 rounded-xl text-sm font-bold flex items-center justify-center gap-2"
                  >
                    <Navigation size={16} /> Directions
                  </a>
                </div>
                <button
                  onClick={() => setAddOpen(true)}
                  className="w-full bg-white/10 hover:bg-white/15 text-white py-2.5 rounded-xl text-sm font-bold flex items-center justify-center gap-2"
                >
                  <CalendarPlus size={16} className="text-emerald-400" /> Add to Schedule again
                </button>

                <p className="text-[10px] text-muted flex items-center gap-1">
                  <ShieldCheck size={11} className="text-emerald-400" /> Identity from {providerName(st.provider)}
                  {st.live ? " · live details" : " · saved details"}
                </p>
              </div>
            </>
          )}
        </div>

        {addOpen && (
          <AddToScheduleSheet place={linkRef} onClose={() => setAddOpen(false)} />
        )}
      </div>
    </div>
  );
}
