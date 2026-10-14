// Launch / First-Run Entry — the ONE routing rule AuthGuard needs to know
// about Launch, extracted as a pure function so it is unit-testable without
// rendering React or mocking the router.
//
// P0 entry-flow cleanup: the web/Safari active path no longer routes an
// unauthenticated visitor through /welcome — that produced a visible,
// overlapping-branding glitch (the animated splash mark still fading/scaling
// out while /login's own static mark + heading mounted underneath it),
// especially on real iPhone Safari via a Quick Tunnel. /welcome/page.tsx,
// its CSS, and the launchSeen device-marker helpers are left fully intact
// and reachable by direct navigation — they are simply no longer part of
// this active routing decision. A future native-shell-specific entry flow
// (Capacitor splash -> auth decision -> Login/Today) can still use them.
//
// Current behavior:
//   - unauthenticated at "/"                                              -> /login
//   - unauthenticated at any OTHER path (deep link to a protected route)  -> /login?next=<path>
//
// This function is only ever meant to be called when the caller already has a
// DEFINITIVE "unauthenticated" status (a real 401), never for "loading" or
// "offline" — AuthGuard itself enforces that.
export function unauthenticatedDestination(pathname: string | null | undefined): string {
  const path = pathname || "/";
  const next = path !== "/" ? `?next=${encodeURIComponent(path)}` : "";
  return `/login${next}`;
}
