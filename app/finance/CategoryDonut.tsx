"use client";

// Phase 07E (Figma 255:130 "Spending"): the Overview's primary frame shows
// spending as flat category rows (icon chip + name + real amount), not a
// donut chart. The prop contract (slices/periodLabel/total — all real
// CategorySlice data) is unchanged; only the rendering changed. The file
// keeps its original name to avoid touching its one import site.

import React from "react";
import { Utensils, Car, ShoppingBag, HeartPulse, Receipt, Film, GraduationCap, Plane, Sparkles } from "lucide-react";
import type { CategorySlice } from "../lib/types";
import { rm } from "../lib/money";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

// Fixed, readable tint + icon keyed by canonical category (not color-only —
// every row always shows the text label + real amount too).
const CATEGORY_META: Record<string, { tint: string; icon: React.ReactNode }> = {
  Food: { tint: "bg-[#f3e5cf]", icon: <Utensils size={14} /> },
  Transport: { tint: "bg-[#ddebe6]", icon: <Car size={14} /> },
  Shopping: { tint: "bg-[#e7e1f0]", icon: <ShoppingBag size={14} /> },
  "Personal Care": { tint: "bg-accent-lav", icon: <Sparkles size={14} /> },
  Bills: { tint: "bg-[#f3e5cf]", icon: <Receipt size={14} /> },
  Entertainment: { tint: "bg-[#e7e1f0]", icon: <Film size={14} /> },
  Health: { tint: "bg-[#ddebe6]", icon: <HeartPulse size={14} /> },
  Education: { tint: "bg-[#ddebe6]", icon: <GraduationCap size={14} /> },
  Travel: { tint: "bg-tile-context", icon: <Plane size={14} /> },
  Other: { tint: "bg-inset", icon: <Receipt size={14} /> },
};
const DEFAULT_META = CATEGORY_META.Other;

export default function CategoryDonut({
  slices,
  periodLabel,
  total,
}: {
  slices: CategorySlice[];
  periodLabel: string;
  total: number;
}) {
  return (
    <div>
      <div className="flex items-center justify-between px-1">
        <h2
          className="text-[20px] font-extrabold text-primary"
          style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
        >
          Spending
        </h2>
        <span className="text-[13px] font-semibold text-secondary" style={{ fontFamily: BODY_FONT }}>
          {periodLabel} · {rm(total)}
        </span>
      </div>

      {(!slices || slices.length === 0) ? (
        <p className="mt-3 text-sm text-muted text-center py-4 bg-surface border border-line rounded-[16px]" style={{ fontFamily: BODY_FONT }}>
          No expenses recorded {periodLabel.toLowerCase()} yet.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {slices.map((s) => {
            const meta = CATEGORY_META[s.category] ?? DEFAULT_META;
            return (
              <li key={s.category} className="flex items-center gap-3 bg-surface border border-line rounded-[16px] px-3 py-2.5">
                <span className={`shrink-0 h-7 w-7 rounded-[9px] flex items-center justify-center text-primary ${meta.tint}`}>
                  {meta.icon}
                </span>
                <span className="min-w-0 flex-1 text-[14px] font-semibold text-primary truncate" style={{ fontFamily: BODY_FONT }}>
                  {s.category}
                </span>
                <span className="shrink-0 text-[14px] font-semibold text-primary tabular-nums" style={{ fontFamily: BODY_FONT }}>
                  {rm(s.amount)}
                </span>
                <span className="shrink-0 w-9 text-right text-[11px] text-muted tabular-nums" style={{ fontFamily: BODY_FONT }}>
                  {s.pct}%
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
