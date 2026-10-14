"use client";
import { useRouter } from "next/navigation";
import React, { useEffect, useMemo, useState } from "react";
import { ArrowRight, ChevronRight, RefreshCw, Wallet as WalletIcon, Activity as ActivityIcon } from "lucide-react";
import { useApiData } from "./lib/useApi";
import { useAuth } from "./lib/auth";
import { rm } from "./lib/money";
import OmniBar from "./components/OmniBar";
import { buildTodaySlides } from "./lib/todayHighlights";
import type { DashboardData } from "./lib/types";

// Today (Figma 226:130) uses the same already-loaded display/body faces as
// the rest of MONOA (app/lib/fonts.ts) — Bricolage Grotesque for headlines,
// Hanken Grotesk for body/label text — applied locally here (not via the
// shared .monoaPageTitle class, whose weight/size are tuned for OTHER pages'
// titles and are left untouched).
const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

/** "TUE, 12 NOV" from the backend's authoritative MYT "today" date — never
 * the device clock, so it can never disagree with what the rest of Today
 * (open tasks, spend, priority) was computed against. */
function todayDateLabel(isoDate: string | undefined): string {
  if (!isoDate) return "";
  const d = new Date(`${isoDate}T12:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  return d
    .toLocaleDateString("en-US", { weekday: "short", day: "numeric", month: "short" })
    .toUpperCase();
}

/** A restrained, always-correct time-of-day greeting from the viewer's own
 * device clock (no server round-trip, no timezone ambiguity). */
function timeOfDayWord(): string {
  const h = new Date().getHours();
  if (h < 5) return "night";
  if (h < 12) return "morning";
  if (h < 17) return "afternoon";
  return "evening";
}

export default function Dashboard() {
  const router = useRouter();
  const { user } = useAuth();
  const { data, error, reload } = useApiData<DashboardData>("/api/dashboard-data");
  const [slideIndex, setSlideIndex] = useState(0);

  // Today must never keep showing a stale "today" after the app was left open
  // or backgrounded across a day boundary (or any other long idle period) —
  // there is no polling here, so refetch once when the tab/app becomes visible
  // again. Cheap and safe: useApiData's reload() is abort-safe and idempotent.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [reload]);

  const firstName = user?.name?.trim().split(/\s+/)[0] || "";
  const greeting = firstName ? `Good ${timeOfDayWord()}, ${firstName}.` : `Good ${timeOfDayWord()}.`;

  const slides = useMemo(
    () =>
      data
        ? buildTodaySlides({
            schedule_status: data.schedule_status,
            schedule_error: data.schedule_error,
            next_event_today: data.next_event_today,
            priority: data.priority,
            hero_insight: data.hero_insight,
          })
        : [],
    [data],
  );
  const activeSlide = slides[Math.min(slideIndex, Math.max(slides.length - 1, 0))];

  const activityCount = data?.activity?.active_recommendations.length ?? 0;
  const activitySubtitle =
    data?.activity_status === "error"
      ? "Unavailable"
      : activityCount > 0
        ? (data?.activity?.active_recommendations[0].message ?? "")
        : "Find places for your next activity.";

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-40"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          {/* header — greeting + date; avatar is the shared, already-positioned AvatarLink */}
          <header className="pr-14">
            <p
              className="text-[11px] font-semibold tracking-wide text-faint"
              style={{ fontFamily: BODY_FONT }}
            >
              {data ? todayDateLabel(data.date) : ""}
            </p>
            <h1
              className="mt-1 text-[26px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              {greeting}
            </h1>
          </header>

          {/* Today Highlight — a real, deterministic carousel over whatever
              genuine signals exist right now (never a fixed fake count of slides).
              Phase 08: this card's bg is a fixed literal #1b2130, not the
              reactive `--fg` token — `bg-white/NN` would resolve to the exact
              same hex in light mode (globals.css maps `white` -> `--fg`,
              which equals #1b2130 in light mode) and disappear. Buttons in
              this card use literal rgba() white instead, deliberately. */}
          <div className="mt-5">
            {error ? (
              <div className="rounded-[28px] bg-[#1b2130] border border-white/5 p-6">
                <p className="text-[15px] leading-relaxed text-[#faf9f4]" style={{ fontFamily: BODY_FONT }}>
                  Couldn&apos;t load your day.
                </p>
                <p className="mt-1 text-[12px] text-[#94a6c2]" style={{ fontFamily: BODY_FONT }}>
                  {error}
                </p>
                <button
                  type="button"
                  onClick={reload}
                  className="mt-4 inline-flex items-center gap-2 min-h-[44px] px-4 rounded-full bg-[rgba(255,255,255,0.10)] border border-[rgba(255,255,255,0.15)] text-sm font-medium text-[#faf9f4] hover:bg-[rgba(255,255,255,0.15)] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#99d9e0]"
                >
                  <RefreshCw size={16} /> Try again
                </button>
              </div>
            ) : activeSlide ? (
              // Phase 10 (Candidate 1): the common real-world case is exactly
              // one slide with no subtitle (e.g. a single open-task count) —
              // at the old 188px minimum, mt-auto below pushed the pill footer
              // to the very bottom, leaving a large, wasted-looking dark gap
              // between the title and the pill. min-h is a FLOOR only: a
              // richer slide (2-line title + subtitle + multi-slide footer)
              // still grows past it exactly as before — this only shrinks the
              // dead space for short, single-slide content.
              <div className="relative rounded-[28px] bg-[#1b2130] border border-white/5 shadow-[0px_5px_16px_0px_rgba(0,0,0,0.15)] px-[22px] pt-5 pb-[18px] min-h-[152px] flex flex-col">
                <p
                  className="text-[10px] font-semibold tracking-wide text-[#94a6c2]"
                  style={{ fontFamily: BODY_FONT }}
                >
                  {activeSlide.eyebrow}
                </p>
                <p
                  className="mt-2 text-[24px] leading-[1.2] font-extrabold text-[#faf9f4] pr-10"
                  style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
                >
                  {activeSlide.title}
                </p>
                {activeSlide.subtitle && (
                  <p className="mt-2 text-[13px] text-[#bac2d1]" style={{ fontFamily: BODY_FONT }}>
                    {activeSlide.subtitle}
                  </p>
                )}

                <div className="mt-auto pt-4 flex items-end justify-between">
                  <div className="flex items-center gap-2.5">
                    {activeSlide.pill &&
                      (activeSlide.route ? (
                        <button
                          type="button"
                          onClick={() => router.push(activeSlide.route!)}
                          aria-label={`Open ${activeSlide.pill}`}
                          className="inline-flex items-center h-7 px-3 rounded-full bg-[#29334a] text-[9px] font-semibold tracking-wide text-[#b8c9e5] hover:bg-[#34405c] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#99d9e0]"
                          style={{ fontFamily: BODY_FONT }}
                        >
                          {activeSlide.pill}
                        </button>
                      ) : (
                        <span
                          className="inline-flex items-center h-7 px-3 rounded-full bg-[#29334a] text-[9px] font-semibold tracking-wide text-[#b8c9e5]"
                          style={{ fontFamily: BODY_FONT }}
                        >
                          {activeSlide.pill}
                        </span>
                      ))}
                    {slides.length > 1 && (
                      <div className="flex items-center gap-1">
                        {slides.map((s, i) => (
                          <button
                            key={s.key}
                            type="button"
                            onClick={() => setSlideIndex(i)}
                            aria-label={`Show highlight ${i + 1} of ${slides.length}`}
                            aria-current={i === slideIndex}
                            className={`h-[6px] rounded-full transition-all ${
                              i === slideIndex ? "w-[18px] bg-[#b8c9e5]" : "w-[6px] bg-[#596378]"
                            }`}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                  {slides.length > 1 && (
                    <div className="flex items-center gap-2">
                      <span
                        className="text-[10px] font-medium text-[#a3adbf]"
                        style={{ fontFamily: BODY_FONT }}
                      >
                        {slideIndex + 1} / {slides.length}
                      </span>
                      <button
                        type="button"
                        onClick={() => setSlideIndex((i) => (i + 1) % slides.length)}
                        aria-label="Next highlight"
                        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[rgba(255,255,255,0.10)] text-[#faf9f4] hover:bg-[rgba(255,255,255,0.15)] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#99d9e0]"
                      >
                        <ChevronRight size={18} />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="rounded-[28px] bg-[#1b2130] border border-white/5 p-6 min-h-[152px] flex items-center">
                <p className="text-[15px] text-[#bac2d1]" style={{ fontFamily: BODY_FONT }}>
                  Loading your day…
                </p>
              </div>
            )}
          </div>

          {/* At a glance */}
          <h2
            className="mt-8 text-[20px] font-extrabold text-primary"
            style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
          >
            At a glance
          </h2>
          <div className="mt-4 flex gap-3.5">
            <div className="flex-1 min-w-0 rounded-[20px] bg-surface border border-line shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] p-3.5">
              <div className="h-9 w-9 rounded-[11px] bg-tile-wallet flex items-center justify-center">
                <WalletIcon size={16} className="text-primary" />
              </div>
              <p className="mt-2.5 text-[10px] font-medium text-secondary" style={{ fontFamily: BODY_FONT }}>
                Spending today
              </p>
              <p
                className="text-[22px] font-extrabold text-primary leading-tight"
                style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
              >
                {data && data.finance_status === "error"
                  ? "—"
                  : data && data.total_spending != null
                    ? rm(data.total_spending)
                    : "—"}
              </p>
              <p className="mt-1 text-[9px] text-faint truncate" style={{ fontFamily: BODY_FONT }}>
                {data && data.finance_status === "error"
                  ? "Unavailable"
                  : data?.finance && data.finance.daily_budget > 0
                    ? data.finance.daily_over_budget
                      ? "Over today's budget"
                      : `${data.finance.daily_used_pct.toFixed(0)}% of daily budget`
                    : "No daily budget set"}
              </p>
            </div>

            <button
              type="button"
              onClick={() => router.push("/activity")}
              className="flex-1 min-w-0 text-left rounded-[20px] bg-surface border border-line shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] p-3.5 hover:bg-surface-2 transition-colors"
            >
              <div className="h-9 w-9 rounded-[11px] bg-accent-lav flex items-center justify-center">
                <ActivityIcon size={16} className="text-primary" />
              </div>
              <p className="mt-2.5 text-[10px] font-medium text-secondary" style={{ fontFamily: BODY_FONT }}>
                Activity
              </p>
              <p
                className="text-[15px] font-semibold text-primary leading-tight"
                style={{ fontFamily: BODY_FONT }}
              >
                {data && data.activity_status === "error"
                  ? "Unavailable"
                  : activityCount > 0
                    ? `${activityCount} to review`
                    : "Explore activities"}
              </p>
              <p className="mt-1 text-[9px] text-faint truncate" style={{ fontFamily: BODY_FONT }}>
                {data ? activitySubtitle : ""}
              </p>
            </button>
          </div>

          {/* Context card — the highest-ranked signal not already the primary
              highlight (today, that is a real pending Activity signal only;
              omitted entirely when there is nothing genuine to say, rather
              than showing an empty/fabricated card). */}
          {data && data.activity_status === "ok" && activityCount > 0 && (
            <button
              type="button"
              onClick={() => router.push("/activity")}
              className="mt-4 w-full text-left rounded-[22px] bg-surface border border-line shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] p-4 flex items-start gap-3 hover:bg-surface-2 transition-colors"
            >
              <div className="h-[42px] w-[42px] shrink-0 rounded-[13px] bg-tile-context flex items-center justify-center">
                <ActivityIcon size={18} className="text-primary" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
                  From Activity
                </p>
                <p className="mt-0.5 text-[11px] text-secondary leading-relaxed" style={{ fontFamily: BODY_FONT }}>
                  {data.activity!.active_recommendations[0].message}
                </p>
                <span className="mt-2 inline-flex items-center gap-1 h-7 px-3 rounded-full bg-strong text-strong-fg text-[10px] font-semibold">
                  View <ArrowRight size={11} />
                </span>
              </div>
            </button>
          )}
        </div>

        {/* Concise Mo entry — the SAME canonical command surface as /mo. Kept
            here even though the current Figma Today frame does not depict a
            composer, since removing it would regress a required, already-
            verified functional affordance (Today must keep supporting a
            direct Mo command without navigating away). */}
        <div className="absolute bottom-24 left-0 w-full px-6 z-30">
          <OmniBar onSuccess={reload} />
        </div>
      </div>
    </div>
  );
}
