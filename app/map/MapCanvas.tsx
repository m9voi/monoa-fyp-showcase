"use client";
// Phase 5D — the real interactive map canvas.
//
// Rendering: Leaflet (BSD-2-Clause, no credential, no telemetry) loaded lazily
// on the client only. Basemap: OpenStreetMap standard raster tiles — free, no
// API key. The server-side Google key is NEVER used or referenced here.
//
// Markers are built ONLY from real data:
//   * the ORIGIN marker is the current search origin — a "you are here" dot for
//     a device fix (with its accuracy circle), or a distinct "search area"
//     marker for a manually chosen area (NO accuracy circle — a manual point is
//     not the user's physical position);
//   * place markers come 1:1 from the current search results.
// No continuous GPS watching. No invented coordinates. No fake basemap.

import { useEffect, useRef } from "react";
import "leaflet/dist/leaflet.css";
import type { CorePlace } from "../lib/types";
import { resultBounds } from "./mapState";

type LeafletModule = typeof import("leaflet");
type LMap = import("leaflet").Map;
type LMarker = import("leaflet").Marker;
type LCircle = import("leaflet").Circle;

interface MapOrigin {
  latitude: number;
  longitude: number;
  accuracyMeters: number | null;
  kind: "device" | "manual";
}

interface MapCanvasProps {
  // Phase 5G: `null` when there is no search origin to show — e.g. the
  // Schedule -> Map "View place" focus view, which must NOT surface the user's
  // current location. The map then centres on the place itself.
  origin: MapOrigin | null;
  places: CorePlace[];
  selectedId: string | null;
  onSelectPlace: (id: string) => void;
  className?: string;
  // B10: fires with the Leaflet map centre AFTER a user-driven pan/zoom settles
  // (Leaflet `moveend` / `zoomend`) — never during a drag, and never for a
  // programmatic recentre (fitBounds / panTo we trigger ourselves). Used only to
  // decide whether to offer "Search this area"; it triggers no network call.
  onCenterSettled?: (center: { latitude: number; longitude: number }) => void;
}

const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors';

function deviceIconHtml(): string {
  return `<span class="block w-3.5 h-3.5 rounded-full bg-blue-500 ring-2 ring-white shadow-[0_0_0_4px_rgba(59,130,246,0.25)]"></span>`;
}
function areaIconHtml(): string {
  // a distinct "search area" marker — an amber target ring, clearly NOT the
  // blue "you are here" dot
  return `<span class="block w-4 h-4 rounded-full border-2 border-amber-400 bg-amber-400/25 shadow-[0_0_0_4px_rgba(251,191,36,0.2)]"></span>`;
}
function placeIconHtml(selected: boolean): string {
  const c = selected ? "#10b981" : "#ffffff";
  const s = selected ? 34 : 26;
  return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="${c}" stroke="#0a0a0a" stroke-width="1.2" style="filter:drop-shadow(0 2px 3px rgba(0,0,0,.5))"><path d="M12 2C8.1 2 5 5.1 5 9c0 5.2 7 13 7 13s7-7.8 7-13c0-3.9-3.1-7-7-7z"/><circle cx="12" cy="9" r="2.4" fill="#0a0a0a"/></svg>`;
}

export default function MapCanvas({
  origin,
  places,
  selectedId,
  onSelectPlace,
  className,
  onCenterSettled,
}: MapCanvasProps) {
  const elRef = useRef<HTMLDivElement | null>(null);
  const LRef = useRef<LeafletModule | null>(null);
  const mapRef = useRef<LMap | null>(null);
  const markersRef = useRef<Map<string, LMarker>>(new Map());
  const originMarkerRef = useRef<LMarker | null>(null);
  const accCircleRef = useRef<LCircle | null>(null);
  const selectRef = useRef(onSelectPlace);
  useEffect(() => {
    selectRef.current = onSelectPlace;
  }, [onSelectPlace]);
  // B10: latest centre callback + a "this move was ours" flag so a programmatic
  // recentre (fitBounds / setView / panTo) never surfaces "Search this area".
  const centerCbRef = useRef(onCenterSettled);
  useEffect(() => {
    centerCbRef.current = onCenterSettled;
  }, [onCenterSettled]);
  const programmaticMoveRef = useRef(false);
  const progClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Mark the NEXT settle as our own move. A safety timer clears the flag even if
  // the move was a no-op and Leaflet never emits `moveend`.
  const markProgrammaticMove = useRef(() => {
    programmaticMoveRef.current = true;
    if (progClearTimerRef.current) clearTimeout(progClearTimerRef.current);
    progClearTimerRef.current = setTimeout(() => {
      programmaticMoveRef.current = false;
    }, 700);
  }).current;

  // ---- init once ---------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const el = elRef.current;
    if (!el) return;
    const markers = markersRef.current;

    const center: [number, number] = origin
      ? [origin.latitude, origin.longitude]
      : places[0]
        ? [places[0].latitude, places[0].longitude]
        : [0, 0];

    void (async () => {
      const L = (await import("leaflet")).default as unknown as LeafletModule;
      if (cancelled || !elRef.current) return;
      LRef.current = L;
      const map = L.map(elRef.current, {
        center,
        zoom: 15,
        zoomControl: true,
        attributionControl: true,
      });
      L.tileLayer(OSM_TILES, { attribution: OSM_ATTR, maxZoom: 19 }).addTo(map);
      mapRef.current = map;
      setTimeout(() => map.invalidateSize(), 0);

      // B10: report the centre only AFTER an interaction settles, and only when
      // it was a real user pan/zoom — not our own fitBounds / panTo (those set
      // lastProgrammaticCenterRef just before moving). Debounced so a
      // zoomend+moveend pair collapses into one report. No network here.
      const reportSettled = () => {
        if (settleTimerRef.current) clearTimeout(settleTimerRef.current);
        settleTimerRef.current = setTimeout(() => {
          if (programmaticMoveRef.current) {
            programmaticMoveRef.current = false;   // consume our own move
            return;
          }
          const c = map.getCenter();
          centerCbRef.current?.({ latitude: c.lat, longitude: c.lng });
        }, 220);
      };
      map.on("moveend", reportSettled);
      map.on("zoomend", reportSettled);
    })();

    const ro = new ResizeObserver(() => mapRef.current?.invalidateSize());
    ro.observe(el);

    return () => {
      cancelled = true;
      ro.disconnect();
      if (settleTimerRef.current) clearTimeout(settleTimerRef.current);
      if (progClearTimerRef.current) clearTimeout(progClearTimerRef.current);
      mapRef.current?.remove();
      mapRef.current = null;
      markers.clear();
      originMarkerRef.current = null;
      accCircleRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- origin marker (device dot vs search-area) + accuracy circle -----
  useEffect(() => {
    const L = LRef.current;
    const map = mapRef.current;
    if (!L || !map) return;
    if (!origin) {
      // no origin to show (focus view) — clear any previous origin marker/circle
      originMarkerRef.current?.remove();
      originMarkerRef.current = null;
      accCircleRef.current?.remove();
      accCircleRef.current = null;
      return;
    }
    const ll: [number, number] = [origin.latitude, origin.longitude];
    const isDevice = origin.kind === "device";

    // rebuild the marker so the icon reflects the current kind
    if (originMarkerRef.current) {
      originMarkerRef.current.remove();
      originMarkerRef.current = null;
    }
    originMarkerRef.current = L.marker(ll, {
      icon: L.divIcon({
        className: "",
        html: isDevice ? deviceIconHtml() : areaIconHtml(),
        iconSize: [isDevice ? 14 : 16, isDevice ? 14 : 16],
        iconAnchor: [isDevice ? 7 : 8, isDevice ? 7 : 8],
      }),
      interactive: false,
      keyboard: false,
      zIndexOffset: 1000,
    }).addTo(map);

    // Accuracy circle ONLY for a device fix that reported a usable accuracy.
    const acc = origin.accuracyMeters;
    if (isDevice && acc != null && Number.isFinite(acc) && acc > 0) {
      const radius = Math.min(Math.max(acc, 20), 5000);
      if (!accCircleRef.current) {
        accCircleRef.current = L.circle(ll, {
          radius,
          color: "#3b82f6",
          weight: 1,
          opacity: 0.4,
          fillColor: "#3b82f6",
          fillOpacity: 0.08,
          interactive: false,
        }).addTo(map);
      } else {
        accCircleRef.current.setLatLng(ll).setRadius(radius);
      }
    } else if (accCircleRef.current) {
      accCircleRef.current.remove();
      accCircleRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [origin?.latitude, origin?.longitude, origin?.accuracyMeters, origin?.kind]);

  // ---- place markers: rebuild by stable place_id ----------------------
  useEffect(() => {
    const L = LRef.current;
    const map = mapRef.current;
    if (!L || !map) return;

    const wanted = new Set(places.map((p) => p.place_id));
    for (const [id, marker] of markersRef.current) {
      if (!wanted.has(id)) {
        marker.remove();
        markersRef.current.delete(id);
      }
    }
    for (const p of places) {
      if (markersRef.current.has(p.place_id)) continue;
      if (!Number.isFinite(p.latitude) || !Number.isFinite(p.longitude)) continue;
      const marker = L.marker([p.latitude, p.longitude], {
        icon: L.divIcon({
          className: "",
          html: placeIconHtml(p.place_id === selectedId),
          iconSize: [26, 26],
          iconAnchor: [13, 24],
        }),
        title: p.name,
        alt: p.name,
        riseOnHover: true,
      });
      marker.on("click", () => selectRef.current(p.place_id));
      marker.on("keypress", () => selectRef.current(p.place_id));
      marker.addTo(map);
      markersRef.current.set(p.place_id, marker);
    }

    if (selectedId === null) {
      const b = resultBounds(
        origin ? { latitude: origin.latitude, longitude: origin.longitude } : null,
        places,
      );
      if (b) {
        markProgrammaticMove();
        if (b[0][0] === b[1][0] && b[0][1] === b[1][1]) {
          map.setView(b[0], 15);
        } else {
          map.fitBounds(b, { padding: [40, 40], maxZoom: 16 });
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [places]);

  // ---- selection: restyle markers + pan to the chosen one ------------
  useEffect(() => {
    const L = LRef.current;
    const map = mapRef.current;
    if (!L || !map) return;
    for (const [id, marker] of markersRef.current) {
      marker.setIcon(
        L.divIcon({
          className: "",
          html: placeIconHtml(id === selectedId),
          iconSize: [26, 26],
          iconAnchor: [13, 24],
        }),
      );
      marker.setZIndexOffset(id === selectedId ? 500 : 0);
    }
    if (selectedId) {
      const m = markersRef.current.get(selectedId);
      if (m) {
        markProgrammaticMove();
        map.panTo(m.getLatLng(), { animate: true });
      }
    }
    // markProgrammaticMove is a stable useRef(...).current
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  return (
    <div
      ref={elRef}
      className={className}
      role="application"
      aria-label={
        !origin
          ? "Map showing the selected place"
          : origin.kind === "device"
            ? "Map of nearby places from your location"
            : "Map of nearby places from the selected area"
      }
    />
  );
}
