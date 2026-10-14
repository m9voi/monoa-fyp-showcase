"use client";
// Who Am I — Product Completion Batch 02. Overview of the four interest
// categories (Games / Sports / Movies & TV / General), each showing how
// many the user has saved so far, linking to its own select/deselect
// screen. The retired "Activities" Figma frame (275:196) is intentionally
// not represented here or anywhere in this flow.
//
// Phase 07F (Figma 264:130): the frame's "This shapes your Today" headline
// and "Interest-based stories and updates appear in Today" copy are not
// truthful for this build — interests are only ever injected into Mo's
// request context (main.py's _mo_interests_context_line), never read by the
// Today/dashboard-data endpoint. The explainer card and footer copy below
// describe the real Mo-only mechanism instead. The per-category preview
// shows the user's REAL selected genre values (never Figma's sample game/
// team/show titles, which this app's allow-list doesn't even contain).

import React from "react";
import Link from "next/link";
import { ChevronLeft, ChevronRight, Gamepad2, Trophy, Clapperboard, Sparkle } from "lucide-react";
import { useApiData } from "../../lib/useApi";
import {
  INTEREST_CATEGORIES,
  INTEREST_CATEGORY_LABELS,
  type InterestCategory,
  type UserInterests,
} from "../../lib/interests";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

const CATEGORY_META: Record<
  InterestCategory,
  { href: string; icon: React.ReactNode; tint: string }
> = {
  game: { href: "/profile/who-am-i/games", icon: <Gamepad2 size={16} />, tint: "bg-accent-lav" },
  sport: { href: "/profile/who-am-i/sports", icon: <Trophy size={16} />, tint: "bg-[#e1ece7]" },
  movie_tv: { href: "/profile/who-am-i/movies-tv", icon: <Clapperboard size={16} />, tint: "bg-[#f1e6d7]" },
  general: { href: "/profile/who-am-i/general", icon: <Sparkle size={16} />, tint: "bg-tile-context" },
};

/** Real selected values, joined for the hub preview — never Figma's sample
 * titles. "Not set" (never blank/fabricated) when nothing is selected yet. */
function previewLabel(values: string[]): string {
  if (!values.length) return "Not set";
  const shown = values.slice(0, 3).join(" · ");
  return values.length > 3 ? `${shown} · +${values.length - 3} more` : shown;
}

export default function WhoAmIPage() {
  const { data, loading } = useApiData<{ interests: UserInterests }>("/api/profile/interests");

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header className="flex items-center gap-4">
            <Link
              href="/profile"
              aria-label="Back to Profile"
              className="w-10 h-10 bg-surface border border-line rounded-full flex items-center justify-center text-secondary hover:text-primary transition-colors shrink-0"
            >
              <ChevronLeft size={20} />
            </Link>
            <h1
              className="text-[22px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Who Am I?
            </h1>
          </header>

          <p className="text-[13px] text-muted" style={{ fontFamily: BODY_FONT }}>
            Tell Mo what you&apos;re into.
          </p>

          <div className="rounded-[22px] bg-[#1b2130] px-5 py-4">
            <p className="text-[16px] font-semibold text-[#fbfaf6]" style={{ fontFamily: BODY_FONT }}>This helps Mo</p>
            <p className="mt-1.5 text-[12px] text-[#d2d6df] leading-relaxed" style={{ fontFamily: BODY_FONT }}>
              Mo uses your interests as light context when it answers — nothing else changes.
            </p>
          </div>

          <h2 className="px-1 text-[18px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>Your interests</h2>

          <div className="space-y-2.5">
            {INTEREST_CATEGORIES.map((cat) => {
              const meta = CATEGORY_META[cat];
              const values = data?.interests[cat] ?? [];
              return (
                <Link
                  key={cat}
                  href={meta.href}
                  className="w-full bg-surface border border-line rounded-[20px] p-3.5 flex items-center gap-3 hover:bg-surface-2 transition-colors min-h-[64px]"
                >
                  <span className={`h-8 w-8 shrink-0 rounded-[10px] flex items-center justify-center text-primary ${meta.tint}`}>
                    {meta.icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-[15px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{INTEREST_CATEGORY_LABELS[cat]}</p>
                    <p className="text-[11px] text-muted truncate" style={{ fontFamily: BODY_FONT }}>
                      {loading && !data ? "…" : previewLabel(values)}
                    </p>
                  </div>
                  <ChevronRight size={18} className="text-muted shrink-0" />
                </Link>
              );
            })}
          </div>

          <p className="text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>
            Visible only to you.
          </p>
        </div>
      </div>
    </div>
  );
}
