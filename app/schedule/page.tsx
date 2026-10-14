"use client";
import React, { useMemo, useState } from "react";
import {
  Plus, MapPin, Repeat, AlertTriangle, CheckCircle2, Circle, CalendarDays, Clock,
} from "lucide-react";
import { useApiData } from "../lib/useApi";
import {
  todayKey, weekOf, fmtDayLabel, fmtLongDate, fmtTime, addMonths, ymd,
} from "../lib/datetime";
import { planCountLabel, lastTimedEventEnd, timelineGaps, gapDurationLabel } from "./planSummary";
import type { ScheduleData, MonthData, EventOccurrence, CoreTask, WeatherConflictsResponse } from "../lib/types";
import MonthCalendar from "./MonthCalendar";
import EventSheet from "./EventSheet";
import TaskSheet from "./TaskSheet";
import { EventDetail, TaskDetail } from "./DetailSheet";
import ConflictResolver from "./ConflictResolver";
import WeatherConflicts from "./WeatherConflicts";
import TaskConflicts from "./TaskConflicts";
import { unresolvedConflicts } from "./conflictView";

// Plan (Figma 238:130) shares Today's already-loaded display/body faces.
const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

type View = "agenda" | "week" | "month";
type Sheet =
  | { kind: "pick" }
  | { kind: "event-new" }
  | { kind: "task-new" }
  | { kind: "event-edit"; id: string }
  | { kind: "task-edit"; task: CoreTask }
  | { kind: "event-detail"; occ: EventOccurrence }
  | { kind: "task-detail"; task: CoreTask }
  | null;

function taskAnchorKey(t: CoreTask): string | null {
  const src = t.planned_at || t.due_date || t.reminder_at;
  if (!src) return null;
  const d = new Date(src);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

const VIEW_LABEL: Record<View, string> = { agenda: "Today", week: "Week", month: "Month" };

export default function SchedulePage() {
  const [view, setView] = useState<View>("agenda");
  const [selected, setSelected] = useState<string>(todayKey());
  const t0 = ymd(todayKey());
  const [cal, setCal] = useState<{ year: number; month: number }>({ year: t0.y, month: t0.m });

  // Agenda / week data
  const dayPath =
    view === "week"
      ? (() => {
          const wk = weekOf(selected);
          return `/api/schedule?date_from=${wk[0]}&date_to=${wk[6]}`;
        })()
      : `/api/schedule/day?date=${selected}`;
  const { data, error, loading, reload } = useApiData<ScheduleData>(dayPath);

  // Month density (only fetched while the month view is active)
  const monthPath = view === "month" ? `/api/schedule/month?year=${cal.year}&month=${cal.month}` : null;
  const { data: monthData, reload: reloadMonth } = useApiData<MonthData>(monthPath);

  // B5: contextual weather warnings over the caller's near-term OUTDOOR events.
  // Its own endpoint (one provider-backed call) so the schedule read stays fast.
  const { data: weather, reload: reloadWeather } =
    useApiData<WeatherConflictsResponse>("/api/schedule/weather-conflicts", true, "provider");

  const reloadAll = () => { reload(); reloadMonth(); reloadWeather(); };

  const events = useMemo(() => data?.events ?? [], [data]);
  const tasks = useMemo(() => data?.tasks ?? [], [data]);
  const conflicts = useMemo(() => data?.conflicts ?? [], [data]);
  const allEvents = data?.all_events ?? [];

  const openConflicts = useMemo(() => unresolvedConflicts(conflicts), [conflicts]);

  const conflictMap = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const c of openConflicts) {
      const aId = c.a?.id, bId = c.b?.id;
      if (!aId || !bId) continue;
      if (!m.has(aId)) m.set(aId, new Set());
      if (!m.has(bId)) m.set(bId, new Set());
      m.get(aId)!.add(c.b?.title ?? bId);
      m.get(bId)!.add(c.a?.title ?? aId);
    }
    return m;
  }, [openConflicts]);

  const [sheet, setSheet] = useState<Sheet>(null);

  const weekDays = weekOf(selected);
  // Figma 238:130's day-strip shows the 5 weekdays of the selected week
  // (Mon–Fri). A weekend `selected` (reached via Week/Month) has no chip
  // shown as active — a known, honest edge case, not a fabricated 6th/7th day.
  const weekdayStrip = weekDays.slice(0, 5);

  // items for the currently-selected day (agenda + month views share this)
  const dayEvents = events
    .filter((e) => e.occurrence_date === selected && e.status !== "cancelled")
    .sort((a, b) => a.start.localeCompare(b.start));
  const dayTasks = tasks.filter((t) => taskAnchorKey(t) === selected || (!taskAnchorKey(t) && !t.is_completed && selected === todayKey()));
  const timedTasks = dayTasks.filter((t) => Boolean(t.planned_at));
  const untimedTasks = dayTasks.filter((t) => !t.planned_at);

  const gaps = useMemo(
    () => timelineGaps(dayEvents.map((e) => ({ id: e.id, start: e.start, end: e.end }))),
    [dayEvents],
  );
  const freeAfter = lastTimedEventEnd(dayEvents);
  const summaryLine =
    planCountLabel(dayEvents.length, dayTasks.length) + (freeAfter ? ` · Free after ${fmtTime(freeAfter)}` : "");

  type TimelineEntry =
    | { kind: "event"; time: string; occ: EventOccurrence }
    | { kind: "task"; time: string; task: CoreTask }
    | { kind: "gap"; time: string; afterId: string; start: string; end: string; minutes: number };
  const timelineEntries: TimelineEntry[] = useMemo(() => {
    const out: TimelineEntry[] = [
      ...dayEvents.map((occ): TimelineEntry => ({ kind: "event", time: occ.start, occ })),
      ...timedTasks.map((task): TimelineEntry => ({ kind: "task", time: task.planned_at as string, task })),
      ...gaps.map((g): TimelineEntry => ({ kind: "gap", time: g.start, ...g })),
    ];
    out.sort((a, b) => a.time.localeCompare(b.time));
    return out;
  }, [dayEvents, timedTasks, gaps]);

  const headerDate = view === "week" ? `Week of ${fmtDayLabel(weekDays[0])}` : fmtLongDate(`${selected}T12:00:00+08:00`);
  const dayName = selected === todayKey() ? "Today" : fmtDayLabel(`${selected}T12:00:00+08:00`).replace(/,.*/, "");

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-40"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          {/* header — title + view switch */}
          <header className="flex items-center justify-between gap-3 pr-14">
            <h1
              className="text-[26px] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Plan
            </h1>
            <div className="flex items-center bg-surface border border-line rounded-[18px] p-[3px] shrink-0">
              {(["agenda", "week", "month"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  aria-pressed={view === v}
                  className={`px-3 h-[30px] rounded-[15px] text-[10px] font-semibold transition-colors ${
                    view === v ? "bg-[#1b2130] text-[#faf9f4]" : "text-secondary hover:text-primary"
                  }`}
                  style={{ fontFamily: BODY_FONT }}
                >
                  {VIEW_LABEL[v]}
                </button>
              ))}
            </div>
          </header>
          <p className="sr-only" aria-live="polite">{headerDate}</p>

          {/* day-strip — real weekdays of the selected week (agenda view only) */}
          {view === "agenda" && (
            <div className="mt-5 grid grid-cols-5 gap-2">
              {weekdayStrip.map((d) => {
                const isSelected = d === selected;
                const n = events.filter((e) => e.occurrence_date === d && e.status !== "cancelled").length
                  + tasks.filter((t) => taskAnchorKey(t) === d).length;
                return (
                  <button
                    key={d}
                    type="button"
                    onClick={() => setSelected(d)}
                    aria-pressed={isSelected}
                    className={`h-[62px] rounded-[18px] flex flex-col items-center justify-center gap-1 transition-colors ${
                      isSelected ? "bg-[#1b2130] text-[#faf9f4] shadow-[0px_5px_16px_0px_rgba(0,0,0,0.08)]" : "bg-surface text-primary"
                    }`}
                  >
                    <span
                      className={`text-[10px] font-medium ${isSelected ? "text-[#c7cbd6]" : "text-faint"}`}
                      style={{ fontFamily: BODY_FONT }}
                    >
                      {fmtDayLabel(d).slice(0, 3)}
                    </span>
                    <span className="text-[16px] font-semibold" style={{ fontFamily: BODY_FONT }}>
                      {Number(d.slice(8, 10))}
                    </span>
                    <span className={`h-1 w-1 rounded-full ${n > 0 ? (isSelected ? "bg-[#99d9e0]" : "bg-[#2f8f9d]") : "bg-transparent"}`} />
                  </button>
                );
              })}
            </div>
          )}

          {view === "week" && (
            <div className="mt-5 grid grid-cols-7 gap-1">
              {weekDays.map((d) => {
                const n = events.filter((e) => e.occurrence_date === d && e.status !== "cancelled").length
                  + tasks.filter((t) => taskAnchorKey(t) === d).length;
                return (
                  <button
                    key={d}
                    onClick={() => setSelected(d)}
                    className={`rounded-xl py-2 flex flex-col items-center gap-1 text-xs ${
                      d === selected ? "bg-strong text-strong-fg font-semibold" : d === todayKey() ? "bg-surface-2 text-primary" : "text-secondary hover:bg-surface-2"
                    }`}
                  >
                    <span className="uppercase text-[9px] tracking-wider">{fmtDayLabel(d).slice(0, 3)}</span>
                    <span>{Number(d.slice(8, 10))}</span>
                    <span className={`w-1 h-1 rounded-full ${n ? (d === selected ? "bg-strong-fg" : "bg-success/80") : "bg-transparent"}`} />
                  </button>
                );
              })}
            </div>
          )}

          {view === "month" && (
            <div className="mt-5">
              <MonthCalendar
                year={cal.year}
                month={cal.month}
                data={monthData ?? null}
                selected={selected}
                onSelect={(d) => { setSelected(d); }}
                onPrev={() => setCal((c) => addMonths(c.year, c.month, -1))}
                onNext={() => setCal((c) => addMonths(c.year, c.month, 1))}
                onToday={() => { const t = ymd(todayKey()); setCal({ year: t.y, month: t.m }); setSelected(todayKey()); }}
              />
            </div>
          )}

          {/* day heading + real summary line */}
          <div className="mt-6">
            <h2
              className="text-[21px] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              {dayName}
            </h2>
            {!loading && !error && (
              <p className="mt-0.5 text-[12px] text-secondary" style={{ fontFamily: BODY_FONT }}>
                {summaryLine}
              </p>
            )}
          </div>

          {/* Conflicts stay their own clearly-labeled section — safety-relevant,
              never folded silently into the timeline. Shown ABOVE the timeline
              so a decision the user needs to make is never missed by scrolling
              past it. */}
          <div className="mt-4 space-y-2.5">
            <ConflictResolver conflicts={conflicts} onResolved={reloadAll} />
            <TaskConflicts conflicts={data?.task_conflicts ?? []} onResolved={reloadAll} />
            <WeatherConflicts
              conflicts={weather?.conflicts ?? []}
              providerUnavailable={weather?.provider_unavailable}
              attribution={weather?.attribution}
              onResolved={reloadAll}
            />
          </div>

          {loading && (
            <p className="text-secondary text-sm text-center py-10" style={{ fontFamily: BODY_FONT }}>
              Loading your plan…
            </p>
          )}
          {!loading && error && (
            <p className="text-danger text-sm text-center py-10 bg-danger/10 border border-danger/20 rounded-2xl mt-4">
              Couldn&apos;t load your plan. Please try again.
            </p>
          )}

          {!loading && !error && (
            <div className="mt-5">
              {timelineEntries.length === 0 && untimedTasks.length === 0 ? (
                <div className="bg-surface border border-line rounded-[20px] p-6 text-center space-y-3">
                  <CalendarDays size={22} className="mx-auto text-faint" />
                  <p className="text-sm text-secondary" style={{ fontFamily: BODY_FONT }}>No plans here yet.</p>
                  <div className="flex gap-2 justify-center">
                    <button onClick={() => setSheet({ kind: "event-new" })} className="bg-surface-2 hover:bg-inset text-primary text-xs font-medium px-4 py-2 rounded-xl">Add event</button>
                    <button onClick={() => setSheet({ kind: "task-new" })} className="bg-surface-2 hover:bg-inset text-primary text-xs font-medium px-4 py-2 rounded-xl">Add task</button>
                  </div>
                </div>
              ) : (
                <>
                  {timelineEntries.length > 0 && (
                    <div className="relative pl-[68px]">
                      {/* Line x and dot x are deliberately the same coordinate
                          (57px / -14px from each row's own left edge, given
                          the 68px gutter) so every dot sits exactly centered
                          on the continuous line, regardless of row height. */}
                      <div className="absolute left-[57px] top-2 bottom-2 w-px bg-line" aria-hidden="true" />
                      <div className="space-y-3">
                        {timelineEntries.map((entry) => {
                          if (entry.kind === "gap") {
                            return (
                              <div key={`gap-${entry.afterId}`} className="relative">
                                <span className="absolute -left-[60px] top-1/2 -translate-y-1/2 w-[42px] text-right text-[10px] font-medium text-faint" style={{ fontFamily: BODY_FONT }}>
                                  {fmtTime(entry.start)}
                                </span>
                                <span className="absolute -left-[14px] top-1/2 -translate-y-1/2 h-[7px] w-[7px] rounded-full bg-line-strong" aria-hidden="true" />
                                <div className="bg-app border border-line rounded-[18px] h-[66px] pl-[12px] pr-4 flex items-center gap-3">
                                  <div className="h-10 w-10 rounded-[12px] bg-surface flex items-center justify-center shrink-0">
                                    <Clock size={16} className="text-faint" />
                                  </div>
                                  <div className="min-w-0">
                                    <p className="text-[15px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>Free</p>
                                    <p className="text-[11px] text-secondary" style={{ fontFamily: BODY_FONT }}>{gapDurationLabel(entry.minutes)}</p>
                                  </div>
                                </div>
                              </div>
                            );
                          }
                          if (entry.kind === "task") {
                            const t = entry.task;
                            return (
                              <div key={`task-${t.id}`} className="relative">
                                <span className="absolute -left-[60px] top-1/2 -translate-y-1/2 w-[42px] text-right text-[10px] font-medium text-faint" style={{ fontFamily: BODY_FONT }}>
                                  {fmtTime(t.planned_at)}
                                </span>
                                <span className="absolute -left-[14px] top-1/2 -translate-y-1/2 h-[7px] w-[7px] rounded-full bg-line-strong" aria-hidden="true" />
                                <button
                                  type="button"
                                  onClick={() => setSheet({ kind: "task-detail", task: t })}
                                  className="w-full text-left bg-surface rounded-[18px] shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] h-[66px] pl-[12px] pr-4 flex items-center gap-3 hover:bg-surface-2 transition-colors"
                                >
                                  <div className="h-10 w-10 rounded-[12px] bg-accent-lav flex items-center justify-center shrink-0">
                                    {t.is_completed ? <CheckCircle2 size={17} className="text-primary" /> : <Circle size={17} className="text-primary" />}
                                  </div>
                                  <div className="min-w-0">
                                    <p className={`text-[15px] font-semibold ${t.is_completed ? "text-faint line-through" : "text-primary"}`} style={{ fontFamily: BODY_FONT }}>
                                      {t.title}
                                    </p>
                                    <p className="text-[11px] text-secondary" style={{ fontFamily: BODY_FONT }}>
                                      Task · {fmtTime(t.planned_at)}
                                    </p>
                                  </div>
                                </button>
                              </div>
                            );
                          }
                          const occ = entry.occ;
                          const cw = Array.from(conflictMap.get(occ.id) ?? []);
                          return (
                            <div key={`event-${occ.id}-${occ.start}`} className="relative">
                              <span className="absolute -left-[60px] top-1/2 -translate-y-1/2 w-[42px] text-right text-[10px] font-medium text-faint" style={{ fontFamily: BODY_FONT }}>
                                {occ.all_day ? "" : fmtTime(occ.start)}
                              </span>
                              <span className="absolute -left-[14px] top-1/2 -translate-y-1/2 h-[7px] w-[7px] rounded-full bg-[#2f8f9d]" aria-hidden="true" />
                              <button
                                type="button"
                                onClick={() => setSheet({ kind: "event-detail", occ })}
                                className="w-full text-left bg-surface rounded-[18px] shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] min-h-[66px] py-3 pl-[12px] pr-4 flex items-center gap-3 hover:bg-surface-2 transition-colors"
                              >
                                <div className="h-10 w-10 rounded-[12px] bg-tile-context flex items-center justify-center shrink-0">
                                  {occ.recurrence && occ.recurrence !== "none" ? (
                                    <Repeat size={16} className="text-primary" />
                                  ) : occ.location || occ.place ? (
                                    <MapPin size={16} className="text-primary" />
                                  ) : (
                                    <Clock size={16} className="text-primary" />
                                  )}
                                </div>
                                <div className="min-w-0 flex-1">
                                  <p className="text-[15px] font-semibold text-primary truncate" style={{ fontFamily: BODY_FONT }}>
                                    {occ.title}
                                  </p>
                                  <p className="text-[11px] text-secondary truncate" style={{ fontFamily: BODY_FONT }}>
                                    {occ.all_day ? "All day" : `${fmtTime(occ.start)} – ${fmtTime(occ.end)}`}
                                    {occ.location ? ` · ${occ.location}` : ""}
                                  </p>
                                  {cw.length > 0 && (
                                    <p className="text-[11px] text-warning mt-1 flex items-center gap-1">
                                      <AlertTriangle size={11} /> Overlaps with {cw.join(", ")}
                                    </p>
                                  )}
                                </div>
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {untimedTasks.length > 0 && (
                    <div className={timelineEntries.length > 0 ? "mt-5 space-y-2.5" : "space-y-2.5"}>
                      <h3 className="text-[11px] font-bold uppercase tracking-widest text-faint" style={{ fontFamily: BODY_FONT }}>
                        Tasks
                      </h3>
                      {untimedTasks.map((t) => (
                        <button
                          key={t.id}
                          type="button"
                          onClick={() => setSheet({ kind: "task-detail", task: t })}
                          className="w-full text-left bg-surface rounded-[18px] shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)] p-3.5 flex items-center gap-3 hover:bg-surface-2 transition-colors"
                        >
                          {t.is_completed ? <CheckCircle2 size={18} className="text-success shrink-0" /> : <Circle size={18} className="text-faint shrink-0" />}
                          <div className="min-w-0">
                            <p className={`text-[14px] font-semibold ${t.is_completed ? "text-faint line-through" : "text-primary"}`} style={{ fontFamily: BODY_FONT }}>
                              {t.title}
                            </p>
                            <p className="text-[11px] text-secondary" style={{ fontFamily: BODY_FONT }}>
                              Task{t.due_date ? " · has due date" : ""}
                            </p>
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        {/* floating add control (Figma 238:193) — the sole entry point for
            adding an event or task, replacing the old inline "+ Add" row. */}
        <button
          type="button"
          onClick={() => setSheet({ kind: "pick" })}
          aria-label="Add event or task"
          className="fixed bottom-24 right-6 z-30 h-12 w-12 rounded-full bg-[#1b2130] text-[#faf9f4] shadow-[0px_5px_16px_0px_rgba(0,0,0,0.15)] flex items-center justify-center hover:opacity-90 transition-opacity focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#99d9e0]"
        >
          <Plus size={24} />
        </button>
      </div>

      {/* sheets */}
      {sheet?.kind === "pick" && (
        <div className="fixed inset-0 z-[100] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4" onClick={() => setSheet(null)} role="dialog" aria-modal="true" aria-label="Add">
          <div className="w-full max-w-md bg-surface border border-line rounded-t-3xl sm:rounded-3xl p-6 space-y-3" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-primary">What are you adding?</h2>
            <button onClick={() => setSheet({ kind: "event-new" })} className="w-full text-left bg-surface-2 hover:bg-inset rounded-2xl p-4">
              <p className="text-sm font-medium text-primary">Event</p>
              <p className="text-xs text-secondary">A time-based commitment — meeting, class, session.</p>
            </button>
            <button onClick={() => setSheet({ kind: "task-new" })} className="w-full text-left bg-surface-2 hover:bg-inset rounded-2xl p-4">
              <p className="text-sm font-medium text-primary">Task</p>
              <p className="text-xs text-secondary">Something to get done — with an optional due date.</p>
            </button>
          </div>
        </div>
      )}

      {sheet?.kind === "event-new" && (
        <EventSheet mode="create" defaultDateKey={selected} tasks={tasks} onClose={() => setSheet(null)} onSaved={reloadAll} />
      )}
      {sheet?.kind === "task-new" && (
        <TaskSheet mode="create" defaultDateKey={selected} onClose={() => setSheet(null)} onSaved={reloadAll} />
      )}
      {sheet?.kind === "event-edit" && (
        <EventSheet
          mode="edit"
          initial={allEvents.find((e) => e.id === sheet.id) ?? null}
          tasks={tasks}
          onClose={() => setSheet(null)}
          onSaved={reloadAll}
        />
      )}
      {sheet?.kind === "task-edit" && (
        <TaskSheet mode="edit" initial={sheet.task} onClose={() => setSheet(null)} onSaved={reloadAll} />
      )}
      {sheet?.kind === "event-detail" && (
        <EventDetail
          occ={sheet.occ}
          conflictWith={Array.from(conflictMap.get(sheet.occ.id) ?? [])}
          relatedTaskTitle={tasks.find((t) => t.id === sheet.occ.related_task_id)?.title ?? null}
          onClose={() => setSheet(null)}
          onEdit={() => setSheet({ kind: "event-edit", id: sheet.occ.id })}
        />
      )}
      {sheet?.kind === "task-detail" && (
        <TaskDetail
          task={sheet.task}
          onClose={() => setSheet(null)}
          onEdit={() => setSheet({ kind: "task-edit", task: sheet.task })}
          onChanged={reloadAll}
        />
      )}
    </div>
  );
}
