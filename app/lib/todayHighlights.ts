// Today (Figma 226:130) — pure slide-construction logic for the "Today
// Highlight" carousel. Purely presentational: it never invents a signal,
// it only re-shapes the REAL fields dashboard-data already returns
// (next_event_today, priority[], hero_insight, schedule_status) into an
// ordered list of slides for the UI to render. No new prioritization rule,
// no learned/personalized copy — the ranking itself still comes entirely
// from the backend's deterministic priority[] array.

export interface TodaySource {
  schedule_status: "ok" | "error";
  schedule_error: string | null;
  next_event_today: { title: string; start: string } | null;
  priority: Array<{ rank: number; kind: string; message: string }>;
  hero_insight: string;
}

export interface TodaySlide {
  key: string;
  eyebrow: string;
  title: string;
  subtitle: string | null;
  pill: string | null;
  /** Where the pill navigates when tapped, or null when there is nowhere
   * useful to send the user (e.g. the schedule-error slide). */
  route: string | null;
}

// Matches 519:2 (Adaptive Smart Stack): a conflict is the one real signal
// that genuinely "needs a decision" — every other kind keeps the normal
// 226:130 "TODAY HIGHLIGHT" eyebrow. This is contextual labeling driven by
// the already-computed real `kind`, not invented personalization.
const PRIORITY_EYEBROW: Record<string, string> = {
  conflict: "NEEDS YOUR DECISION",
  event_soon: "TODAY HIGHLIGHT",
  budget_over: "BUDGET",
  budget_near: "BUDGET",
  activity: "ACTIVITY",
  open_task: "TASKS",
};

const PRIORITY_PILL: Record<string, string> = {
  conflict: "PLAN",
  event_soon: "PLAN",
  budget_over: "WALLET",
  budget_near: "WALLET",
  activity: "ACTIVITY",
  open_task: "PLAN",
};

const PILL_ROUTE: Record<string, string> = {
  PLAN: "/schedule",
  WALLET: "/finance",
  ACTIVITY: "/activity",
};

/** "10:00 AM" from a real ISO datetime, or null if it can't be parsed. */
export function formatSlideClock(iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

/**
 * Build the ordered list of real slides for the Today Highlight carousel.
 * Never empty: when there is truly nothing to say, one honest fallback
 * slide carries the plain `hero_insight` summary — the same text the
 * pre-visual UI always showed.
 */
export function buildTodaySlides(data: TodaySource): TodaySlide[] {
  const slides: TodaySlide[] = [];

  if (data.schedule_status === "error") {
    slides.push({
      key: "schedule-error",
      eyebrow: "SCHEDULE",
      title: data.schedule_error ?? "Couldn't load your schedule right now.",
      subtitle: null,
      pill: null,
      route: null,
    });
  } else if (data.next_event_today) {
    slides.push({
      key: "next-event",
      eyebrow: "TODAY HIGHLIGHT",
      title: data.next_event_today.title,
      subtitle: formatSlideClock(data.next_event_today.start),
      pill: "PLAN",
      route: PILL_ROUTE.PLAN,
    });
  }

  for (const p of data.priority) {
    // Redundant with the next-event slide above (same event, same fact).
    if (p.kind === "event_soon" && data.next_event_today) continue;
    const pill = PRIORITY_PILL[p.kind] ?? null;
    slides.push({
      key: `priority-${p.kind}`,
      eyebrow: PRIORITY_EYEBROW[p.kind] ?? "TODAY HIGHLIGHT",
      title: p.message,
      subtitle: null,
      pill,
      route: pill ? (PILL_ROUTE[pill] ?? null) : null,
    });
  }

  if (slides.length === 0) {
    slides.push({
      key: "fallback",
      eyebrow: "TODAY HIGHLIGHT",
      title: data.hero_insight,
      subtitle: null,
      pill: null,
      route: null,
    });
  }

  return slides;
}
