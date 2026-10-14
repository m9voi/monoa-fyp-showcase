"use client";

// The ONE mobile primary navigation bar. Rendered once in AppChrome for every
// authenticated screen — the per-page hand-built bars have been removed.
// Desktop (>= md) uses the Sidebar instead, so this is hidden there.
//
// - real <Link>s, visible labels under every symbol
// - active destination from usePathname (correct after client nav AND refresh)
// - >= 44x44 targets, safe-area bottom padding, visible focus ring
// - restrained press feedback + a one-shot "pop" on the newly-active item,
//   both disabled under prefers-reduced-motion (see globals.css)

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ITEMS, isNavActive } from "../lib/nav";
import { NAV_ICONS, MoMark } from "./NavIcons";

// Figma 226:130 "bottom-nav": a 78px bar, four plain icon+label items, and a
// raised 46px Mo disc that sits ~7px above the other icons' baseline. The
// side items keep their existing real Lucide-translated icons (Figma's own
// export for those came back as text-glyph placeholders, not real icon
// assets, so there is nothing faithful to copy there) — only sizing/label
// scale/the Mo disc treatment are adjusted to match Figma's measurements.
export default function BottomNav() {
  const pathname = usePathname() || "/";

  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-nav-border bg-nav/95 backdrop-blur-xl lg:hidden"
    >
      <ul
        className="mx-auto flex max-w-md items-stretch justify-around px-2 pt-1.5"
        style={{ paddingBottom: "max(0.375rem, env(safe-area-inset-bottom))" }}
      >
        {NAV_ITEMS.map((item) => {
          const active = isNavActive(pathname, item.href);
          const Icon = NAV_ICONS[item.key];
          return (
            <li key={item.key} className="flex">
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`group flex min-h-[44px] min-w-[44px] flex-col items-center gap-1 rounded-xl px-2 py-1 text-[9px] font-medium leading-none transition-transform duration-150 active:scale-[0.92] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white/70 ${
                  item.center ? "justify-start -mt-2" : "justify-center"
                }`}
              >
                <span className={active ? "monoaNavPop" : undefined}>
                  {item.center ? (
                    <MoMark className="h-[46px] w-[46px] drop-shadow-[0_2px_6px_rgba(0,0,0,0.18)]" />
                  ) : (
                    <Icon active={active} className={`h-6 w-6 ${active ? "text-white" : "text-muted"}`} />
                  )}
                </span>
                <span className={`${item.center ? "mt-1" : ""} ${active ? "text-white" : "text-muted"}`}>
                  {item.label}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
