// Phase 5D — device-location acquisition sub-state + the invariants that keep it
// from getting stuck. Pure and DOM-free so the guarantees are unit-tested
// (app/map/__tests__/deviceLoc.test.ts) rather than only reasoned about.

import type { LocationErrorStatus } from "../lib/location";

/** Only meaningful while a device fix is being acquired. */
export type DeviceLoc =
  | { phase: "idle" }
  | { phase: "requesting" }
  | { phase: "failed"; status: LocationErrorStatus; message: string };

/**
 * Strict-Mode-safe body for the "is this component still mounted?" effect.
 *
 * React (development, and Strict Mode) replays effects: setup → cleanup → setup.
 * A cleanup-ONLY effect
 *
 *     const mounted = useRef(true);
 *     useEffect(() => () => { mounted.current = false; }, []);
 *
 * leaves `mounted.current === false` permanently after that replay, even though
 * the component is mounted and on screen. Every `if (!mounted.current) return;`
 * guard in an async continuation then bails forever — so state that should
 * transition out of "requesting" never does, and the UI hangs on
 * "Getting your location…".
 *
 * The fix is to ESTABLISH the flag on every setup, not just clear it on
 * cleanup. Use as: `useEffect(() => mountedEffect(mounted), []);`
 */
export function mountedEffect(ref: { current: boolean }): () => void {
  ref.current = true;
  return () => {
    ref.current = false;
  };
}

/**
 * Independent page-level acquisition deadline. Longer than the helper's own
 * watchdog (LOCATION_WATCHDOG_MS) plus the /api/location/validate abort budget,
 * so the helper's more specific result normally wins; this only catches a true
 * wedge (helper never settles, React lifecycle misbehaves, fetch hangs).
 */
export const LOCATION_ACQUIRE_DEADLINE_MS = 30_000;

/**
 * The page-level fail-safe transition. If we are still "requesting" when the
 * deadline fires, move to a truthful, recoverable TIMEOUT — never a fabricated
 * position, never a hardcoded fallback location. Idempotent: anything that is
 * not still "requesting" is returned unchanged.
 */
export function acquireFailsafe(prev: DeviceLoc, timeoutMessage: string): DeviceLoc {
  return prev.phase === "requesting"
    ? { phase: "failed", status: "TIMEOUT", message: timeoutMessage }
    : prev;
}
