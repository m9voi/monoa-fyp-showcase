// Canonical primary navigation — the SINGLE source of truth for label, route,
// order and active-state across the mobile BottomNav and the desktop Sidebar.
// Pure data + one pure predicate (no JSX, no hooks) so it is unit-testable with
// `node --test`. Icon components are mapped by `key` in app/components/NavIcons.tsx.
//
// Authoritative IA (B1): Today · Plan · Mo · Activity · Wallet.
//   Today    -> /
//   Plan     -> /schedule
//   Mo       -> /mo         (centre destination; official MONOA Mo mark)
//   Activity -> /activity
//   Wallet   -> /finance
//
// Map (/map) is intentionally NOT a primary destination — it stays reachable
// through contextual Schedule / place actions.

export type NavKey = "today" | "plan" | "mo" | "activity" | "wallet";

export interface NavItem {
  key: NavKey;
  label: string;
  href: string;
  /** The centre slot — rendered with the fixed official Mo mark. */
  center?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { key: "today", label: "Today", href: "/" },
  { key: "plan", label: "Plan", href: "/schedule" },
  { key: "mo", label: "Mo", href: "/mo", center: true },
  { key: "activity", label: "Activity", href: "/activity" },
  { key: "wallet", label: "Wallet", href: "/finance" },
];

/**
 * Is `href` the active primary destination for the current `pathname`?
 *
 *   "/"      matches ONLY the exact "/" (otherwise it would match everything).
 *   "/x"     matches "/x" and any nested "/x/..." (e.g. /finance -> /finance/history).
 */
export function isNavActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}
