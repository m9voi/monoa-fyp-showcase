// CoreOS — location foundation (Phase 5B).
//
// One typed, reusable service for acquiring the user's REAL device location.
// Pure helpers (validation / error normalisation / freshness) are exported
// separately so they can be unit-tested without a browser.
//
// PRIVACY BOUNDARY
//   * Coordinates returned here live only in the caller's React state (in memory).
//   * They are sent to the backend ONLY inside a POST body (never a URL/query).
//   * They are never persisted, never written to logs, never passed to the AI.
//   * There is NO location-history storage anywhere.
//   See docs/secure-development.md §10.
//
// There is NO hardcoded fallback location. If acquisition fails, the caller
// surfaces the specific failure state — never a default city.

// --------------------------------------------------------------------------- //
// state model
// --------------------------------------------------------------------------- //
export type LocationStatus =
  | "NOT_REQUESTED"
  | "REQUESTING"
  | "AVAILABLE"
  | "SERVICES_OFF"          // B11.1: the account's Location Services preference is OFF —
                            // MONOA has NOT called navigator.geolocation. Distinct from
                            // PERMISSION_DENIED (that IS the browser/OS refusing).
  | "PERMISSION_DENIED"
  | "POSITION_UNAVAILABLE"
  | "TIMEOUT"
  | "INSECURE_CONTEXT"
  | "UNSUPPORTED"
  | "INVALID_COORDINATES"
  | "BACKEND_UNREACHABLE"
  | "UNKNOWN_ERROR";

// The error subset getCurrentLocation() itself can produce. BACKEND_UNREACHABLE
// is produced by callers doing the /api/location/validate round-trip, not here.
export type LocationErrorStatus = Exclude<
  LocationStatus,
  "NOT_REQUESTED" | "REQUESTING" | "AVAILABLE"
>;

export interface Coordinates {
  latitude: number;
  longitude: number;
  /** metres; null when the device did not report a usable accuracy figure */
  accuracyMeters: number | null;
  /** epoch ms of the fix */
  timestamp: number;
}

export interface LocationAvailable {
  status: "AVAILABLE";
  coordinates: Coordinates;
}

export interface LocationError {
  status: LocationErrorStatus;
  /** plain, user-facing copy — never a raw error code */
  message: string;
  /** true if a fresh attempt could plausibly succeed */
  retryable: boolean;
}

export type LocationResult = LocationAvailable | LocationError;

// --------------------------------------------------------------------------- //
// policy constants  (see report §F for the reasoning)
// --------------------------------------------------------------------------- //

/** How long to wait for a fix. The old Map used 4 000 ms, far too short for a
 *  cold mobile GPS fix; 15 s balances responsiveness against reality. This is
 *  passed to the browser as the `timeout` option — but see LOCATION_WATCHDOG_MS:
 *  that option is NOT a reliable upper bound. */
export const LOCATION_TIMEOUT_MS = 15_000;

/** CoreOS-owned hard upper bound on a single acquisition attempt.
 *
 *  The browser's own `timeout` option cannot be trusted to settle the call:
 *   * per the Geolocation spec the timeout does NOT count down while a
 *     permission prompt is pending, so a user who ignores the prompt waits
 *     forever;
 *   * some engines / OS location providers never invoke EITHER callback.
 *
 *  Without an independent watchdog `getCurrentLocation()` could stay unresolved
 *  and the UI would sit in "Getting your location…" indefinitely. This watchdog
 *  guarantees the promise always settles. It is set a little ABOVE
 *  LOCATION_TIMEOUT_MS so that when the browser's own timeout does fire we
 *  surface its more specific result first. On expiry we resolve to a truthful
 *  TIMEOUT error — never a fabricated coordinate. */
export const LOCATION_WATCHDOG_MS = 20_000;

/** A cached fix newer than this may be reused as "current" — the user has not
 *  moved far in 2 min and nearby-place search only needs neighbourhood accuracy.
 *  Also passed as `maximumAge` so a retry does not force a fresh GPS spin-up. */
export const LOCATION_MAX_AGE_MS = 120_000;

/** Above this the fix is only city-level. Still usable for nearby search — used
 *  for an optional UI note, never to reject a location. */
export const LOCATION_COARSE_ACCURACY_M = 5_000;

/** Above this the fix is far coarser than a nearby-search radius (default 8 km),
 *  so stating a confident metre-level "X m away" from it would mislead. Results
 *  still show — with a clear caveat and the distance figure suppressed. Never a
 *  rejection reason; no fallback coordinate is ever substituted. */
export const LOCATION_UNRELIABLE_ACCURACY_M = 20_000;

/** enableHighAccuracy default. Nearby-place search needs neighbourhood accuracy,
 *  not centimetre navigation. `false` is faster, lower-battery and works better
 *  indoors. A caller may override for a genuine precision need. */
export const LOCATION_HIGH_ACCURACY_DEFAULT = false;

// --------------------------------------------------------------------------- //
// user-facing copy + retry policy
// --------------------------------------------------------------------------- //
const MESSAGES: Record<LocationErrorStatus, string> = {
  SERVICES_OFF:
    "Location Services is turned off for your MONOA account. Turn it on in Settings to use your current location. You can still search for a place by name.",
  // Phase 10 (Candidate 2): the old copy just said "Turn it on for this site
  // in Settings" — ambiguous right after a user has just been to MONOA's OWN
  // Settings screen and turned Location Services on there. This is a
  // DIFFERENT, OS/browser-level permission MONOA cannot grant or detect
  // beyond what the browser reports; name it explicitly so the two are never
  // conflated, and give the concrete iPhone/Safari steps since a same-page
  // retry cannot recover this (see locationRecoveryAction below).
  PERMISSION_DENIED:
    "Your browser or iPhone is blocking location for this site — that's separate from the Location Services toggle in MONOA's own Settings. On iPhone: open Safari's “aA” menu → Website Settings → Location → Allow, then reload. You can still search for a place by name.",
  POSITION_UNAVAILABLE: "Your location isn't available right now.",
  TIMEOUT: "We couldn't get your location in time.",
  INSECURE_CONTEXT: "Location needs a secure connection.",
  UNSUPPORTED: "Location isn't supported in this browser.",
  INVALID_COORDINATES: "Your device reported an invalid location.",
  BACKEND_UNREACHABLE: "Couldn't reach the server. Please try again.",
  UNKNOWN_ERROR: "Something went wrong getting your location.",
};

const RETRYABLE: Record<LocationErrorStatus, boolean> = {
  SERVICES_OFF: false, // a same-page retry can't change an account preference
  PERMISSION_DENIED: false, // will not change until the user edits site settings
  INSECURE_CONTEXT: false, // environment/config, not transient
  UNSUPPORTED: false,
  POSITION_UNAVAILABLE: true,
  TIMEOUT: true,
  INVALID_COORDINATES: true,
  BACKEND_UNREACHABLE: true,
  UNKNOWN_ERROR: true,
};

export function locationErrorMessage(status: LocationErrorStatus): string {
  return MESSAGES[status];
}

export function isRetryable(status: LocationErrorStatus): boolean {
  return RETRYABLE[status];
}

/**
 * What the "location failed" panel's PRIMARY recovery control should do.
 *
 *   "retry"  — a transient failure (timeout, position unavailable, a backend
 *              blip): calling getCurrentLocation() again in the same page can
 *              succeed, so offer "Try again".
 *   "reload" — a non-retryable failure (permission denied, insecure context,
 *              unsupported): a same-page retry CANNOT recover it. In particular,
 *              iOS Safari does not re-read a site's location permission for an
 *              already-loaded page after the user changes it in Settings — the
 *              page must be reloaded. Offer "Reload" instead of a dead retry.
 *
 * The secondary actions ("Search a location", "Back") work regardless and are
 * unaffected.
 */
export function locationRecoveryAction(
  status: LocationErrorStatus,
): "retry" | "reload" | "settings" {
  // B11.1: Location Services OFF is fixed only by changing an account
  // preference — neither a same-page retry nor a page reload helps, so point
  // the user at Settings. (Manual "Search a location" stays available too.)
  if (status === "SERVICES_OFF") return "settings";
  return isRetryable(status) ? "retry" : "reload";
}

export function makeLocationError(status: LocationErrorStatus): LocationError {
  return { status, message: MESSAGES[status], retryable: RETRYABLE[status] };
}

// --------------------------------------------------------------------------- //
// pure helpers
// --------------------------------------------------------------------------- //
export function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export interface CoordinateValidation {
  ok: boolean;
  coordinates: Coordinates | null;
}

/**
 * Validate raw lat/lng/accuracy. Rejects non-finite lat/lng (NaN, ±Infinity) and
 * out-of-range values. A non-finite / negative accuracy does NOT fail the fix —
 * it is normalised to null (a usable location is still a usable location).
 */
export function validateCoordinates(
  latitude: unknown,
  longitude: unknown,
  accuracy: unknown,
  timestamp?: unknown,
): CoordinateValidation {
  if (!isFiniteNumber(latitude) || !isFiniteNumber(longitude)) {
    return { ok: false, coordinates: null };
  }
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
    return { ok: false, coordinates: null };
  }
  const accuracyMeters = isFiniteNumber(accuracy) && accuracy >= 0 ? accuracy : null;
  const ts = isFiniteNumber(timestamp) && timestamp > 0 ? timestamp : Date.now();
  return {
    ok: true,
    coordinates: { latitude, longitude, accuracyMeters, timestamp: ts },
  };
}

/** Map a GeolocationPositionError (or its numeric `code`) to a location state.
 *  Codes: 1 PERMISSION_DENIED, 2 POSITION_UNAVAILABLE, 3 TIMEOUT. */
export function normalizeGeolocationError(err: unknown): LocationError {
  let code: unknown;
  if (typeof err === "number") {
    code = err;
  } else if (typeof err === "object" && err !== null && "code" in err) {
    code = (err as { code: unknown }).code;
  }
  if (code === 1) return makeLocationError("PERMISSION_DENIED");
  if (code === 2) return makeLocationError("POSITION_UNAVAILABLE");
  if (code === 3) return makeLocationError("TIMEOUT");
  return makeLocationError("UNKNOWN_ERROR");
}

/** Is a fix with this timestamp recent enough to treat as "current"? */
export function isLocationFresh(timestamp: number, now: number = Date.now()): boolean {
  if (!isFiniteNumber(timestamp) || timestamp <= 0) return false;
  const age = now - timestamp;
  return age >= 0 && age <= LOCATION_MAX_AGE_MS;
}

/** Coarse (city-level) fix? Informational only — never a rejection reason. */
export function isAccuracyCoarse(accuracyMeters: number | null): boolean {
  return accuracyMeters !== null && accuracyMeters > LOCATION_COARSE_ACCURACY_M;
}

/** Fix too coarse for a trustworthy "nearby" distance claim. Callers should
 *  label the location as very approximate and stop showing confident metre
 *  distances. Still never a rejection reason. */
export function isAccuracyUnreliable(accuracyMeters: number | null): boolean {
  return isFiniteNumber(accuracyMeters) && accuracyMeters > LOCATION_UNRELIABLE_ACCURACY_M;
}

// --------------------------------------------------------------------------- //
// environment checks (feature detection, not UA sniffing)
// --------------------------------------------------------------------------- //
export function isGeolocationSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.geolocation !== "undefined" &&
    typeof navigator.geolocation.getCurrentPosition === "function"
  );
}

/** Only reports insecure when we can POSITIVELY determine it. An engine without
 *  window.isSecureContext is not assumed insecure — the geolocation call will
 *  surface the real failure. */
export function isInsecureContext(): boolean {
  return (
    typeof window !== "undefined" &&
    "isSecureContext" in window &&
    window.isSecureContext === false
  );
}

// --------------------------------------------------------------------------- //
// permission state (best-effort, NON-authoritative, not polled)
// --------------------------------------------------------------------------- //
export type PermissionStateValue = "granted" | "prompt" | "denied" | "unknown";

export async function readPermissionState(): Promise<PermissionStateValue> {
  try {
    if (
      typeof navigator === "undefined" ||
      typeof navigator.permissions === "undefined" ||
      typeof navigator.permissions.query !== "function"
    ) {
      return "unknown";
    }
    const status = await navigator.permissions.query({
      name: "geolocation" as PermissionName,
    });
    return status.state === "granted" ||
      status.state === "prompt" ||
      status.state === "denied"
      ? status.state
      : "unknown";
  } catch {
    // Permissions API absent / throwing (Safari quirks) — the acquisition
    // result stays authoritative.
    return "unknown";
  }
}

// --------------------------------------------------------------------------- //
// the acquisition call
// --------------------------------------------------------------------------- //
export interface GetLocationOptions {
  timeoutMs?: number;
  maxAgeMs?: number;
  highAccuracy?: boolean;
  /** Override the CoreOS watchdog (ms). 0 disables it — only for tests that
   *  drive the callbacks synchronously. Never disabled in the app. */
  watchdogMs?: number;
}

/**
 * Acquire the user's current location ONCE. Never rejects — always resolves to a
 * typed LocationResult. No internal retries (one call = at most one permission
 * prompt). This uses a single-shot position read, not a continuous position
 * watch: CoreOS needs a point-in-time fix for place search, not live tracking.
 */
export function getCurrentLocation(
  opts: GetLocationOptions = {},
): Promise<LocationResult> {
  if (!isGeolocationSupported()) {
    return Promise.resolve(makeLocationError("UNSUPPORTED"));
  }
  if (isInsecureContext()) {
    return Promise.resolve(makeLocationError("INSECURE_CONTEXT"));
  }

  const timeout = opts.timeoutMs ?? LOCATION_TIMEOUT_MS;
  const maximumAge = opts.maxAgeMs ?? LOCATION_MAX_AGE_MS;
  const enableHighAccuracy = opts.highAccuracy ?? LOCATION_HIGH_ACCURACY_DEFAULT;
  const watchdogMs = opts.watchdogMs ?? LOCATION_WATCHDOG_MS;

  return new Promise<LocationResult>((resolve) => {
    let settled = false;
    // Plain browser timer handle — standard timer semantics only, no
    // Node-runtime-specific handle methods.
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    // Settle-once. Clears the watchdog and makes every later callback
    // (a slow success, a late error, a duplicate) a no-op.
    const done = (r: LocationResult): void => {
      if (settled) return;
      settled = true;
      if (watchdog !== undefined) clearTimeout(watchdog);
      resolve(r);
    };

    // CoreOS-owned watchdog: the single guarantee that this promise settles even
    // if the browser never calls either callback. Resolves to a truthful
    // TIMEOUT (retryable) — no fabricated position.
    if (watchdogMs > 0 && typeof setTimeout === "function") {
      watchdog = setTimeout(() => done(makeLocationError("TIMEOUT")), watchdogMs);
    }

    try {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const v = validateCoordinates(
            pos.coords.latitude,
            pos.coords.longitude,
            pos.coords.accuracy,
            pos.timestamp,
          );
          if (!v.ok || v.coordinates === null) {
            done(makeLocationError("INVALID_COORDINATES"));
            return;
          }
          done({ status: "AVAILABLE", coordinates: v.coordinates });
        },
        (err) => done(normalizeGeolocationError(err)),
        { enableHighAccuracy, timeout, maximumAge },
      );
    } catch {
      // getCurrentPosition itself threw (very old engines).
      done(makeLocationError("UNKNOWN_ERROR"));
    }
  });
}
