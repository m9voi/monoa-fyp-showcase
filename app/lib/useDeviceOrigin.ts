"use client";
// Phase 5D/5E — the ONE shared device-location acquisition hook.
//
// Composes the shared primitives (no geolocation is reimplemented here):
//   * getCurrentLocation()      — app/lib/location.ts (single-shot + watchdog)
//   * SearchOrigin              — app/map/searchOrigin.ts (device | manual)
//   * DeviceLoc / mountedEffect / acquireFailsafe — app/map/deviceLoc.ts
//
// Guarantees carried from Phase 5D:
//   * `deviceLoc.phase === "requesting"` can never persist (helper watchdog +
//     an independent page-level deadline).
//   * a stale async continuation never writes state (request token + a
//     Strict-Mode-safe mountedRef flag).
//   * `/api/location/validate` is abort-bounded.
//   * a manual origin invalidates any in-flight device acquisition; a late
//     device callback can never overwrite it.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { useAuth } from "./auth";
import { useSettings } from "../components/ThemeProvider";
import { getCurrentLocation, locationErrorMessage } from "./location";
import type { LocationValidateResponse } from "./types";
import type { SearchOrigin } from "../map/searchOrigin";
import {
  type DeviceLoc,
  mountedEffect,
  acquireFailsafe,
  LOCATION_ACQUIRE_DEADLINE_MS,
} from "../map/deviceLoc";

export interface UseDeviceOrigin {
  origin: SearchOrigin | null;
  deviceLoc: DeviceLoc;
  /** Acquire the device fix, validate it, set it as the origin. `onOrigin`
   *  fires exactly once on success (before any re-render race). */
  acquireDevice: (opts?: { highAccuracy?: boolean; onOrigin?: (o: SearchOrigin) => void }) => void;
  /** Set an explicit manual origin from a geocoding result; invalidates any
   *  pending device acquisition. */
  setManualOrigin: (r: { latitude: number; longitude: number; label: string }) => SearchOrigin;
  /** Forget the origin and any pending acquisition (e.g. "change activity"). */
  clearOrigin: () => void;
  /** Monotonic request token — callers doing their OWN async work (a place
   *  search) read/bump this so a manual origin change invalidates them too. */
  reqSeqRef: React.MutableRefObject<number>;
  /** Strict-Mode-safe "still mountedRef?" flag for caller async guards. */
  mountedRef: React.MutableRefObject<boolean>;
}

export function useDeviceOrigin(): UseDeviceOrigin {
  const [origin, setOrigin] = useState<SearchOrigin | null>(null);
  const [deviceLoc, setDeviceLoc] = useState<DeviceLoc>({ phase: "idle" });

  // B11.1 — the account's Location Services preference genuinely gates every
  // browser geolocation trigger. `locationServices` is reactive; `ready`
  // guards against acting on the neutral default before the account's real
  // value has been ingested.
  const { locationServices, ready: settingsReady } = useSettings();
  const { status: authStatus, user } = useAuth();

  const reqSeqRef = useRef(0);
  const mountedRef = useRef(true);
  useEffect(() => mountedEffect(mountedRef), []);

  // Independent page-level fail-safe: "requesting" can NEVER persist. Runs in
  // the component, no async closure, no dependency on `mountedRef.current`.
  useEffect(() => {
    if (deviceLoc.phase !== "requesting") return;
    const deadline = setTimeout(() => {
      setDeviceLoc((prev) => acquireFailsafe(prev, locationErrorMessage("TIMEOUT")));
    }, LOCATION_ACQUIRE_DEADLINE_MS);
    return () => clearTimeout(deadline);
  }, [deviceLoc.phase]);

  // Kept in a ref so the gate reads the CURRENT preference without making
  // acquireDevice depend on it (its identity must stay stable for callers).
  const locationServicesRef = useRef(locationServices);
  useEffect(() => { locationServicesRef.current = locationServices; }, [locationServices]);

  const acquireDevice = useCallback<UseDeviceOrigin["acquireDevice"]>((opts) => {
    // B11.1 GATE: if the account's Location Services preference is OFF, MONOA
    // does NOT reach the browser geolocation API at all — it surfaces a distinct,
    // truthful SERVICES_OFF state and leaves manual search available.
    if (locationServicesRef.current === false) {
      reqSeqRef.current++;   // invalidate any in-flight acquisition
      setDeviceLoc({
        phase: "failed",
        status: "SERVICES_OFF",
        message: locationErrorMessage("SERVICES_OFF"),
      });
      return;
    }
    void (async () => {
      const my = ++reqSeqRef.current;
      setDeviceLoc({ phase: "requesting" });
      const result = await getCurrentLocation(
        opts?.highAccuracy ? { highAccuracy: true } : undefined,
      );
      if (my !== reqSeqRef.current || !mountedRef.current) return;
      if (result.status !== "AVAILABLE") {
        setDeviceLoc({ phase: "failed", status: result.status, message: result.message });
        return;
      }
      // Server-authoritative coordinate check (Phase 5B), abort-bounded so a
      // wedged backend surfaces a recoverable state instead of hanging.
      const ctrl = new AbortController();
      const validateTimer = setTimeout(() => ctrl.abort(), 8_000);
      try {
        const v = await apiFetch<LocationValidateResponse>("/api/location/validate", {
          method: "POST",
          signal: ctrl.signal,
          body: JSON.stringify({
            latitude: result.coordinates.latitude,
            longitude: result.coordinates.longitude,
            accuracy_m: result.coordinates.accuracyMeters,
            timestamp_ms: result.coordinates.timestamp,
          }),
        });
        if (my !== reqSeqRef.current || !mountedRef.current) return;
        const o: SearchOrigin = {
          source: "device",
          latitude: v.latitude,
          longitude: v.longitude,
          accuracyMeters: v.accuracy_m,
        };
        setOrigin(o);
        setDeviceLoc({ phase: "idle" });
        opts?.onOrigin?.(o);
      } catch (err) {
        if (my !== reqSeqRef.current || !mountedRef.current) return;
        console.error("Location validation failed:", err);
        setDeviceLoc({
          phase: "failed",
          status: "BACKEND_UNREACHABLE",
          message: "Couldn't reach the server. Please try again.",
        });
      } finally {
        clearTimeout(validateTimer);
      }
    })();
  }, []);

  const setManualOrigin = useCallback<UseDeviceOrigin["setManualOrigin"]>((r) => {
    // Bump the token FIRST so any in-flight acquireDevice() is invalidated and
    // can never call setOrigin({source:"device"}) over this manual origin.
    reqSeqRef.current++;
    const o: SearchOrigin = {
      source: "manual",
      latitude: r.latitude,
      longitude: r.longitude,
      label: r.label,
    };
    setOrigin(o);
    setDeviceLoc({ phase: "idle" });
    return o;
  }, []);

  const clearOrigin = useCallback(() => {
    reqSeqRef.current++;
    setOrigin(null);
    setDeviceLoc({ phase: "idle" });
  }, []);

  // B11.1 — turning Location Services OFF must clear any TRANSIENT device-origin
  // state (a manual origin the user picked stays usable). Only acts once the
  // account's real preference is known. This effect deliberately syncs rendered
  // origin state to a preference change (an external transition).
  useEffect(() => {
    if (!settingsReady || locationServices) return;
    reqSeqRef.current++;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOrigin((prev) => (prev && prev.source === "device" ? null : prev));
    setDeviceLoc((prev) => (prev.phase === "requesting" ? { phase: "idle" } : prev));
  }, [locationServices, settingsReady]);

  // B11.1 — a sign-out or an account switch must never leak the previous user's
  // transient location state into the next session. Syncs rendered state to the
  // auth transition (an external system), matching ThemeProvider's pattern.
  useEffect(() => {
    reqSeqRef.current++;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOrigin(null);
    setDeviceLoc({ phase: "idle" });
  }, [authStatus, user?.id]);

  return { origin, deviceLoc, acquireDevice, setManualOrigin, clearOrigin, reqSeqRef, mountedRef };
}
