"use client";

import React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { monthGrid, monthName, todayKey } from "../lib/datetime";
import type { MonthData } from "../lib/types";

const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function MonthCalendar({
  year,
  month,
  data,
  selected,
  onSelect,
  onPrev,
  onNext,
  onToday,
}: {
  year: number;
  month: number;
  data: MonthData | null;
  selected: string;
  onSelect: (dateKey: string) => void;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
}) {
  const grid = monthGrid(year, month);
  const counts = data?.day_counts ?? {};
  const today = todayKey();

  return (
    <div className="bg-surface border border-white/10 rounded-3xl p-4">
      <div className="flex items-center justify-between mb-3">
        <button onClick={onPrev} aria-label="Previous month" className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-white/10 text-secondary">
          <ChevronLeft size={18} />
        </button>
        <div className="text-center">
          <p className="text-sm font-semibold text-white">{monthName(month)} {year}</p>
          <button onClick={onToday} className="text-[11px] text-emerald-400 hover:text-emerald-300">Today</button>
        </div>
        <button onClick={onNext} aria-label="Next month" className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-white/10 text-secondary">
          <ChevronRight size={18} />
        </button>
      </div>

      <div className="grid grid-cols-7 gap-1 text-center">
        {WD.map((d) => (
          <div key={d} className="text-[10px] font-bold uppercase tracking-wider text-muted py-1">{d}</div>
        ))}
        {grid.flat().map((dateKey) => {
          const inMonth = Number(dateKey.slice(5, 7)) === month;
          const c = counts[dateKey];
          const isToday = dateKey === today;
          const isSelected = dateKey === selected;
          const dots = c ? Math.min((c.events || 0) + (c.tasks || 0), 3) : 0;
          return (
            <button
              key={dateKey}
              onClick={() => onSelect(dateKey)}
              aria-label={dateKey}
              aria-pressed={isSelected}
              className={`aspect-square rounded-xl flex flex-col items-center justify-center gap-1 text-sm transition-colors
                ${isSelected ? "bg-white text-black font-semibold"
                  : isToday ? "bg-white/10 text-white"
                  : inMonth ? "text-primary hover:bg-white/5" : "text-muted hover:bg-white/5"}`}
            >
              <span>{Number(dateKey.slice(8, 10))}</span>
              <span className="flex gap-0.5 h-1">
                {Array.from({ length: dots }).map((_, i) => (
                  <span key={i} className={`w-1 h-1 rounded-full ${isSelected ? "bg-strong-fg" : "bg-emerald-400/80"}`} />
                ))}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
