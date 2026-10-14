import { test } from "node:test";
import assert from "node:assert/strict";

import { buildTodaySlides, formatSlideClock, type TodaySource } from "../todayHighlights.ts";

const BASE: TodaySource = {
  schedule_status: "ok",
  schedule_error: null,
  next_event_today: null,
  priority: [],
  hero_insight: "You have no upcoming events today and no open tasks.",
};

test("empty day falls back to a single honest hero_insight slide", () => {
  const slides = buildTodaySlides(BASE);
  assert.equal(slides.length, 1);
  assert.equal(slides[0].title, BASE.hero_insight);
  assert.equal(slides[0].eyebrow, "TODAY HIGHLIGHT");
  assert.equal(slides[0].pill, null);
});

test("a next event becomes the first real slide with a formatted clock subtitle", () => {
  const data: TodaySource = {
    ...BASE,
    next_event_today: { title: "Design Review", start: "2026-09-20T15:00:00+08:00" },
  };
  const slides = buildTodaySlides(data);
  assert.equal(slides.length, 1);
  assert.equal(slides[0].title, "Design Review");
  assert.equal(slides[0].pill, "PLAN");
  assert.equal(slides[0].route, "/schedule");
  assert.ok(slides[0].subtitle && slides[0].subtitle.length > 0);
});

test("priority items become additional real slides, in the backend's own order", () => {
  const data: TodaySource = {
    ...BASE,
    next_event_today: { title: "Design Review", start: "2026-09-20T15:00:00+08:00" },
    priority: [
      { rank: 0, kind: "conflict", message: "1 schedule conflict needs your decision." },
      { rank: 2, kind: "budget_near", message: "You're at 80% of today's budget." },
    ],
  };
  const slides = buildTodaySlides(data);
  assert.equal(slides.length, 3);
  assert.equal(slides[0].eyebrow, "TODAY HIGHLIGHT"); // next-event slide first
  assert.equal(slides[1].eyebrow, "NEEDS YOUR DECISION");
  assert.equal(slides[1].title, "1 schedule conflict needs your decision.");
  assert.equal(slides[1].route, "/schedule");
  assert.equal(slides[2].eyebrow, "BUDGET");
  assert.equal(slides[2].pill, "WALLET");
  assert.equal(slides[2].route, "/finance");
});

test("an event_soon priority item is never duplicated alongside the next-event slide", () => {
  const data: TodaySource = {
    ...BASE,
    next_event_today: { title: "Design Review", start: "2026-09-20T15:00:00+08:00" },
    priority: [{ rank: 1, kind: "event_soon", message: "Design Review starts soon." }],
  };
  const slides = buildTodaySlides(data);
  assert.equal(slides.length, 1);
  assert.equal(slides[0].key, "next-event");
});

test("a schedule error still surfaces independent finance/activity priority slides", () => {
  const data: TodaySource = {
    ...BASE,
    schedule_status: "error",
    schedule_error: "Couldn't load your schedule right now.",
    priority: [{ rank: 1, kind: "budget_over", message: "You're over today's budget." }],
  };
  const slides = buildTodaySlides(data);
  assert.equal(slides.length, 2);
  assert.equal(slides[0].eyebrow, "SCHEDULE");
  assert.equal(slides[0].title, "Couldn't load your schedule right now.");
  assert.equal(slides[1].eyebrow, "BUDGET");
});

test("formatSlideClock renders a real time and rejects garbage", () => {
  const t = formatSlideClock("2026-09-20T15:00:00+08:00");
  assert.ok(t && /\d/.test(t));
  assert.equal(formatSlideClock("not-a-date"), null);
});
