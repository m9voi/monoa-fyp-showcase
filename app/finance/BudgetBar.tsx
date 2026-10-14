"use client";

import React from "react";
import { rm, clampPct } from "../lib/money";

/**
 * One budget row: label, spent / budget, a progress bar, and a truthful
 * over-budget state. Never color-only — the over-budget case also shows text.
 */
export default function BudgetBar({
  label,
  spent,
  budget,
  remaining,
  usedPct,
  over,
}: {
  label: string;
  spent: number;
  budget: number;
  remaining: number;
  usedPct: number;
  over: boolean;
}) {
  if (!budget || budget <= 0) {
    return (
      <div className="flex items-center justify-between py-2">
        <span className="text-sm text-secondary">{label}</span>
        <span className="text-xs text-muted">No {label.toLowerCase()} budget set</span>
      </div>
    );
  }

  const width = clampPct(usedPct);
  const nearThreshold = !over && width >= 80;

  return (
    <div className="py-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm text-primary">{label}</span>
        <span className="text-xs tabular-nums text-secondary">
          {rm(spent)} <span className="text-muted">/ {rm(budget)}</span>
        </span>
      </div>
      <div className="h-2 w-full rounded-full bg-inset overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${over ? "bg-danger-solid" : nearThreshold ? "bg-warning" : "bg-success-solid"}`}
          style={{ width: `${width}%` }}
        />
      </div>
      <p className={`text-[11px] mt-1 ${over ? "text-danger font-medium" : "text-muted"}`}>
        {over
          ? `Over budget by ${rm(Math.abs(remaining))} (${usedPct}% used)`
          : `${rm(remaining)} left · ${usedPct}% used`}
      </p>
    </div>
  );
}
