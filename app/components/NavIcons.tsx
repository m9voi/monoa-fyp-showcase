"use client";

// Primary-navigation icon set. Icon DIRECTION, active/inactive treatment and the
// restrained pop motion are translated from the approved Antigravity nav
// reference (monoa_clever_obvious_MO.html) into inline SVG — no CDN/runtime code
// is carried over. Inactive = stroked outline; active = the same shape with a
// filled accent. Everything inherits `currentColor` EXCEPT the Mo mark, which
// keeps its fixed official brand colours in every state.

import React from "react";
import type { NavKey } from "../lib/nav";

interface IconProps {
  active?: boolean;
  className?: string;
}

const base = {
  viewBox: "0 0 24 24",
  fill: "none" as const,
  stroke: "currentColor",
  strokeWidth: 2.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** Today — sun. */
function TodayIcon({ active, className }: IconProps) {
  return (
    <svg {...base} className={className}>
      <circle cx="12" cy="12" r="5" fill={active ? "currentColor" : "none"} />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M6.34 6.34l1.42 1.42M16.24 16.24l1.42 1.42M6.34 17.66l1.42-1.42M16.24 7.76l1.42-1.42" />
    </svg>
  );
}

/** Plan — target; active gains a small dart. */
function PlanIcon({ active, className }: IconProps) {
  return (
    <svg {...base} className={className}>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3" fill={active ? "currentColor" : "none"} />
      {active && (
        <>
          <path d="M14 10l7-7" />
          <path d="M17 3h4v4" />
        </>
      )}
    </svg>
  );
}

/** Activity — compass needle. */
function ActivityIcon({ active, className }: IconProps) {
  return (
    <svg {...base} className={className}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 6l2.5 6l-2.5 6l-2.5-6Z" fill={active ? "currentColor" : "none"} />
      {active && <path d="M12 3v3" strokeWidth={3} />}
    </svg>
  );
}

/** Wallet — coin slot. */
function WalletIcon({ active, className }: IconProps) {
  if (active) {
    return (
      <svg {...base} className={className}>
        <path d="M4 16h16" strokeWidth={4} />
        <path d="M8 16a4 4 0 0 1 8 0Z" fill="currentColor" stroke="none" />
      </svg>
    );
  }
  return (
    <svg {...base} className={className}>
      <path d="M4 16h16" />
      <circle cx="12" cy="8" r="4" />
    </svg>
  );
}

/**
 * The official MONOA/Mo mark. FIXED geometry and FIXED brand colours —
 * identical for active and inactive, no glow / gradient / pulse / sparkle /
 * mask / neon, never a Lucide substitute.
 *
 * Phase 08: this used to be a simplified circle + two capsules, which had
 * drifted from the canonical app-icon geometry (also used on /login as of
 * Phase 07H). One shared source of truth now, consistent everywhere this
 * mark appears (BottomNav's center tab, the desktop Sidebar, and Mo's
 * "MO IS READING" indicator all import this same component).
 */
export function MoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" fill="none" aria-hidden="true" className={className}>
      <rect width="100" height="100" rx="22.5" fill="#1b2130" />
      <rect x="8" y="8" width="84" height="84" rx="20" fill="#FF9B71" />
      <rect x="8" y="8" width="84" height="72" rx="20" fill="#99d9e0" />
      <rect x="20" y="20" width="60" height="58" rx="16" fill="#fdfbf7" />
      <rect x="32" y="38" width="10" height="22" rx="5" fill="#1b2130" />
      <rect x="58" y="38" width="10" height="22" rx="5" fill="#1b2130" />
      <path d="M 42 66 Q 50 72 58 66" stroke="#1b2130" strokeWidth="3.5" strokeLinecap="round" fill="none" />
    </svg>
  );
}

/** Mo nav slot — the mark on a fixed pale plate so the brand colours stay
 *  readable on a dark bar (matches how the reference renders it on a card). */
function MoIcon({ className }: IconProps) {
  return (
    <span className={`inline-flex items-center justify-center rounded-full bg-mo-plate ${className ?? ""}`}>
      <MoMark className="h-[70%] w-[70%]" />
    </span>
  );
}

export const NAV_ICONS: Record<NavKey, (p: IconProps) => React.JSX.Element> = {
  today: TodayIcon,
  plan: PlanIcon,
  mo: MoIcon,
  activity: ActivityIcon,
  wallet: WalletIcon,
};
