// Launch / First-Run Entry — local "has this device already seen the MONOA
// intro" marker. This is intentionally the ONLY place that touches
// localStorage for this concern; every caller (AuthGuard, the /welcome page)
// goes through these two functions instead of reading the key directly.
//
// Scope: client-only, device-local. No backend persistence for this phase —
// signing in on a different device (or clearing site data) will show the
// intro again, and that is the intended behavior for now.
//
// Versioned key: bumping LAUNCH_SEEN_KEY lets a future, meaningfully changed
// Launch experience be shown again to everyone without a migration.
const LAUNCH_SEEN_KEY = "monoa.launchSeen.v1";

function getStorage(): Storage | null {
  try {
    // Accessing window.localStorage itself can throw (Safari private mode,
    // storage disabled by policy, etc.) — not just individual get/set calls.
    if (typeof window === "undefined") return null;
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Has this device already completed or skipped the Launch experience?
 *
 * Defaults to `false` (treat as first-time) whenever the marker cannot be
 * read — storage unavailable, disabled, or simply not yet written. That is
 * the safe direction to fail in: at worst a returning visitor sees the
 * (fully skippable) intro again; it never silently hides the feature or
 * fabricates a "returning" state that was never actually recorded.
 */
export function hasSeenLaunch(): boolean {
  const storage = getStorage();
  if (!storage) return false;
  try {
    return storage.getItem(LAUNCH_SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * Record that this device has finished with Launch (via Skip, or by reaching
 * the end of the sequence). Best-effort: if storage is unavailable this is a
 * silent no-op rather than a thrown error — Launch must keep working (and
 * simply replay next time) even in a fully storage-less environment.
 */
export function markLaunchSeen(): void {
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.setItem(LAUNCH_SEEN_KEY, "1");
  } catch {
    /* best-effort — nothing further to do if the write itself fails */
  }
}
