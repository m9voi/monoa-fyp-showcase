"use client";
import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Sparkles, Receipt, Plus, Pencil, Bot } from "lucide-react";
import { apiFetch, ApiError } from "../lib/api";
import { useApiData } from "../lib/useApi";
import { rm, clampPct } from "../lib/money";
import type { FinanceData, FinanceMeta, Transaction } from "../lib/types";
import CategoryDonut from "./CategoryDonut";
import BudgetBar from "./BudgetBar";
import TransactionSheet from "./TransactionSheet";

// Phase 07E (Figma 255:130 "Wallet — Overview" + 474:175 "Limit Warning" +
// 474:253 "Transactions Empty"): Wallet is a personal money-AWARENESS module,
// not a banking dashboard — per the batch's own product-role guidance the
// hero switches from a balance-forward figure to "spent this month vs.
// monthly limit" (Figma's own framing), using the SAME already-real
// FinanceSnapshot fields the old hero used (no new calculation). The
// available-balance figure is preserved, just demoted to a secondary card
// rather than removed. Figma's "Savings" section (a goal-tracking feature
// with its own W10/W11/W15 flow) is deliberately NOT implemented — the
// backend has no savings-goal capability, and the task's own "DO NOT ADD"
// list forbids adding one; see the batch report's out-of-scope section.
const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

// Phase 08: the Overview is a summary, not the ledger — /finance/history is
// the real, existing full list. Display-only; transaction order/persistence
// from /api/finance is untouched.
const RECENT_LIMIT = 6;

export default function FinancePage() {
  const router = useRouter();
  const { data, error, loading, reload } = useApiData<FinanceData>("/api/finance");
  const { data: meta } = useApiData<FinanceMeta>("/api/finance/meta");

  const [balanceInput, setBalanceInput] = useState("");
  const [showBalanceForm, setShowBalanceForm] = useState(false);
  const [dailyInput, setDailyInput] = useState("");
  const [weeklyInput, setWeeklyInput] = useState("");
  const [monthlyInput, setMonthlyInput] = useState("");
  const [showBudgetForm, setShowBudgetForm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedbackMsg, setFeedbackMsg] = useState("");
  const [sheet, setSheet] = useState<null | { mode: "create" | "edit"; tx?: Transaction }>(null);

  const snap = data?.snapshot;
  const transactions = data?.transactions ?? [];
  const recommendations = data?.active_recommendations ?? [];
  const categories = meta?.categories ?? [];

  const flash = (msg: string) => {
    setFeedbackMsg(msg);
    setTimeout(() => setFeedbackMsg(""), 3000);
  };

  const saveSettings = async (payload: Record<string, number>, okMsg: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch("/api/finance/settings", { method: "POST", body: JSON.stringify(payload) });
      reload();
      flash(okMsg);
      return true;
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "Couldn't save. Please try again.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const saveStartingBalance = async () => {
    const v = Number(balanceInput);
    if (!Number.isFinite(v) || v < 0) {
      flash("Enter a valid non-negative amount.");
      return;
    }
    if (await saveSettings({ starting_balance: v }, "Starting balance saved.")) {
      setBalanceInput("");
      setShowBalanceForm(false);
    }
  };

  const saveBudgets = async () => {
    const payload: Record<string, number> = {};
    for (const [k, val] of [
      ["daily_budget", dailyInput],
      ["weekly_budget", weeklyInput],
      ["monthly_budget", monthlyInput],
    ] as const) {
      if (val.trim() === "") continue;
      const n = Number(val);
      if (!Number.isFinite(n) || n < 0) {
        flash("Budgets must be valid non-negative amounts.");
        return;
      }
      payload[k] = n;
    }
    if (Object.keys(payload).length === 0) {
      flash("Enter at least one budget value.");
      return;
    }
    if (await saveSettings(payload, "Budgets saved.")) {
      setDailyInput("");
      setWeeklyInput("");
      setMonthlyInput("");
      setShowBudgetForm(false);
    }
  };

  const updateInsight = async (id: string, action: "ACCEPTED" | "DISMISSED") => {
    setBusy(true);
    try {
      await apiFetch("/api/recommendations/update", {
        method: "POST",
        body: JSON.stringify({ recommendation_id: id, action }),
      });
      reload();
      if (action === "ACCEPTED") flash("Insight acknowledged.");
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "Couldn't update this insight.");
    } finally {
      setBusy(false);
    }
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-MY", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header className="flex items-start justify-between pr-14">
            <div>
              <h1
                className="text-[26px] leading-[1.15] font-extrabold text-primary"
                style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
              >
                Wallet
              </h1>
              {snap && (
                <p className="mt-1 text-[13px] text-muted" style={{ fontFamily: BODY_FONT }}>{snap.period_label}</p>
              )}
            </div>
            {snap && (
              <button
                type="button"
                onClick={() => setSheet({ mode: "create" })}
                aria-label="Add transaction"
                className="shrink-0 h-12 w-12 rounded-full bg-[#1b2130] text-[#fafaf5] flex items-center justify-center hover:opacity-90 transition-opacity"
              >
                <Plus size={22} />
              </button>
            )}
          </header>

          {feedbackMsg && (
            <div className="fixed top-20 left-1/2 -translate-x-1/2 z-50 bg-surface border border-line text-primary text-xs font-medium py-3 px-6 rounded-[16px] text-center shadow-[0px_5px_16px_0px_rgba(0,0,0,0.15)]" style={{ fontFamily: BODY_FONT }}>
              {feedbackMsg}
            </div>
          )}

          {loading && <p className="text-muted text-sm text-center py-10" style={{ fontFamily: BODY_FONT }}>Loading your finances…</p>}
          {!loading && error && (
            <p className="text-danger text-sm text-center py-10 bg-danger/10 border border-danger/20 rounded-[20px]" style={{ fontFamily: BODY_FONT }}>
              Couldn&apos;t load your finances. Please try again.
            </p>
          )}

          {!loading && !error && snap && (
            <>
              {/* Hero: "spent this month vs. monthly limit" — the real
                  awareness metric this product is about, not a bank balance. */}
              <div className="rounded-[28px] bg-[#1b2130] px-[22px] pt-5 pb-[18px]">
                <p className="text-[11px] font-semibold tracking-wide text-[#bfc5d2]" style={{ fontFamily: BODY_FONT }}>SPENT THIS MONTH</p>
                <p
                  className="mt-2 text-[34px] font-extrabold text-[#fafaf5] leading-none"
                  style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
                >
                  {rm(snap.month_spending)}
                </p>
                <p className="mt-2 text-[13px] text-[#d7dbe4]" style={{ fontFamily: BODY_FONT }}>
                  {snap.monthly_budget > 0 ? `of ${rm(snap.monthly_budget)} monthly limit` : "No monthly limit set"}
                </p>
                {snap.monthly_budget > 0 && (
                  <>
                    <div className="mt-3 h-[6px] w-full rounded-full bg-[#434a5a] overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${snap.monthly_over_budget ? "bg-danger-solid" : "bg-[#c8b8e8]"}`}
                        style={{ width: `${clampPct(snap.monthly_used_pct)}%` }}
                      />
                    </div>
                    <p className="mt-2 text-[12px] font-semibold text-[#fafaf5]" style={{ fontFamily: BODY_FONT }}>
                      {snap.monthly_over_budget
                        ? `${rm(Math.abs(snap.monthly_remaining))} over budget`
                        : `${rm(snap.monthly_remaining)} remaining`}
                    </p>
                  </>
                )}
              </div>

              {/* Limit warning (Figma 474:175) — only when real data says the
                  monthly limit is actually exceeded. Transactions still save;
                  this is informational, never a blocker. */}
              {snap.monthly_over_budget && (
                <div className="rounded-[22px] border border-line bg-surface p-[22px]">
                  <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>THIS MONTH</p>
                  <span className="mt-2 inline-flex h-[26px] items-center rounded-[13px] bg-danger/10 px-3 text-[11px] font-semibold text-danger">LIMIT REACHED</span>
                  <p className="mt-2.5 text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
                    {rm(snap.month_spending)} of {rm(snap.monthly_budget)}
                  </p>
                  <p className="mt-1.5 text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>
                    Transactions still save. Review spending or adjust your limit.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <button
                      onClick={() => router.push("/finance/history")}
                      className="bg-[#1b2130] hover:opacity-90 text-[#fafaf5] px-5 py-2.5 rounded-[16px] text-xs font-bold"
                    >
                      Review spending
                    </button>
                    <button
                      onClick={() => setShowBudgetForm(true)}
                      className="border border-line bg-surface hover:bg-surface-2 text-primary px-5 py-2.5 rounded-[16px] text-xs font-bold"
                    >
                      Edit limit
                    </button>
                  </div>
                </div>
              )}

              {/* Balance — preserved real functionality, kept as a secondary
                  (not hero) card so Wallet doesn't read as a bank balance. */}
              <div className="bg-surface border border-line rounded-[20px] p-4 space-y-1">
                <p className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>AVAILABLE BALANCE</p>
                <p className="text-[22px] font-extrabold text-primary" style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}>{rm(snap.available_balance)}</p>
                <p className="text-[11px] text-muted" style={{ fontFamily: BODY_FONT }}>
                  Starting balance {rm(snap.starting_balance)}
                  {snap.total_income > 0 ? ` · income ${rm(snap.total_income)}` : ""}
                  {" · "}spent {rm(snap.total_expenses)}
                </p>
                <button onClick={() => setShowBalanceForm((v) => !v)} className="pt-2 text-[13px] font-semibold text-secondary hover:text-primary" style={{ fontFamily: BODY_FONT }}>
                  {snap.starting_balance > 0 ? "Update starting balance" : "Set starting balance"}
                </button>
                {showBalanceForm && (
                  <div className="pt-1 flex gap-2">
                    <input
                      type="number" min="0" step="0.01" value={balanceInput}
                      onChange={(e) => setBalanceInput(e.target.value)} placeholder="1500.00"
                      aria-label="Starting balance"
                      className="flex-1 bg-field border border-field-border rounded-[14px] px-3 py-2 text-sm text-primary outline-none"
                      style={{ fontFamily: BODY_FONT }}
                    />
                    <button onClick={saveStartingBalance} disabled={busy} className="bg-[#1b2130] text-[#fafaf5] px-4 py-2 rounded-[14px] text-sm font-semibold disabled:opacity-50">
                      {busy ? "Saving…" : "Save"}
                    </button>
                  </div>
                )}
              </div>

              {/* Budgets — real daily/weekly/monthly limits; Figma's hero only
                  visualises the monthly one, but all three are real, verified
                  functionality and stay visible here. */}
              <div className="bg-surface border border-line rounded-[20px] p-4 space-y-1">
                <div className="flex items-center justify-between">
                  <h3 className="text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>BUDGETS</h3>
                  <button onClick={() => setShowBudgetForm((v) => !v)} className="text-xs font-semibold text-secondary hover:text-primary">
                    {showBudgetForm ? "Close" : "Edit"}
                  </button>
                </div>
                <BudgetBar label="Daily" spent={snap.today_spending} budget={snap.daily_budget} remaining={snap.daily_remaining} usedPct={snap.daily_used_pct} over={snap.daily_over_budget} />
                <BudgetBar label="Weekly" spent={snap.week_spending} budget={snap.weekly_budget} remaining={snap.weekly_remaining} usedPct={snap.weekly_used_pct} over={snap.weekly_over_budget} />
                <BudgetBar label="Monthly" spent={snap.month_spending} budget={snap.monthly_budget} remaining={snap.monthly_remaining} usedPct={snap.monthly_used_pct} over={snap.monthly_over_budget} />
                {showBudgetForm && (
                  <div className="pt-3 space-y-2">
                    {[
                      ["Daily", dailyInput, setDailyInput, snap.daily_budget] as const,
                      ["Weekly", weeklyInput, setWeeklyInput, snap.weekly_budget] as const,
                      ["Monthly", monthlyInput, setMonthlyInput, snap.monthly_budget] as const,
                    ].map(([label, val, setVal, cur]) => (
                      <div key={label} className="flex items-center gap-2">
                        <span className="text-xs text-muted w-16" style={{ fontFamily: BODY_FONT }}>{label}</span>
                        <input
                          type="number" min="0" step="0.01" value={val}
                          onChange={(e) => setVal(e.target.value)}
                          placeholder={cur > 0 ? String(cur) : "0.00"}
                          aria-label={`${label} budget`}
                          className="flex-1 bg-field border border-field-border rounded-[14px] px-3 py-2 text-sm text-primary outline-none"
                          style={{ fontFamily: BODY_FONT }}
                        />
                      </div>
                    ))}
                    <button onClick={saveBudgets} disabled={busy} className="w-full bg-[#1b2130] text-[#fafaf5] py-2 rounded-[14px] text-sm font-semibold disabled:opacity-50">
                      {busy ? "Saving…" : "Save budgets"}
                    </button>
                  </div>
                )}
              </div>

              {/* Insights (only real, backend-driven, PENDING ones) */}
              {recommendations.map((rec) => (
                <div key={rec.id} className="bg-info/10 border border-info/20 p-5 rounded-[22px]">
                  <div className="flex items-center gap-2 mb-2.5">
                    <Sparkles size={16} className="text-info" />
                    <h3 className="text-primary font-semibold text-sm" style={{ fontFamily: BODY_FONT }}>Strategic Insight</h3>
                  </div>
                  <p className="text-sm text-primary leading-relaxed mb-4" style={{ fontFamily: BODY_FONT }}>{rec.message}</p>
                  <div className="flex items-center gap-4">
                    <button onClick={() => updateInsight(rec.id, "ACCEPTED")} disabled={busy} className="bg-[#1b2130] text-[#fafaf5] hover:opacity-90 px-5 py-2.5 rounded-full text-sm font-semibold flex items-center gap-2 disabled:opacity-50">
                      Acknowledge <ArrowRight size={16} />
                    </button>
                    <button onClick={() => updateInsight(rec.id, "DISMISSED")} disabled={busy} className="text-muted hover:text-primary text-sm font-medium disabled:opacity-50">
                      Dismiss
                    </button>
                  </div>
                </div>
              ))}

              {/* Spending by category (real data; Figma 255:130's flat rows).
                  Phase 08: its own "See all" was removed — it pointed to the
                  exact same /finance/history route as Recent's link a few
                  rows below, a duplicate affordance for the same destination. */}
              {transactions.length > 0 && (
                <CategoryDonut slices={snap.category_breakdown} periodLabel={snap.period_label} total={snap.month_spending} />
              )}

              {/* Recent transactions — Phase 08: the Overview shows only the
                  most recent RECENT_LIMIT (real data, real order, untouched);
                  "View All" is the one, real destination for the full list. */}
              <div className="space-y-2.5">
                <div className="flex justify-between items-center px-1">
                  <h2
                    className="text-[20px] font-extrabold text-primary"
                    style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
                  >
                    Recent
                  </h2>
                  {transactions.length > 0 && (
                    <button onClick={() => router.push("/finance/history")} className="text-[13px] font-semibold text-secondary hover:text-primary" style={{ fontFamily: BODY_FONT }}>View All</button>
                  )}
                </div>

                {transactions.length === 0 ? (
                  // Figma 474:253 "Transactions Empty" — honest, no fabricated spending.
                  <div className="rounded-[28px] border border-line bg-surface p-6 text-center">
                    <div className="mx-auto h-[50px] w-[50px] rounded-[16px] bg-accent-lav flex items-center justify-center text-primary">
                      <Receipt size={22} />
                    </div>
                    <p className="mt-4 text-[19px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>Your Wallet is ready.</p>
                    <p className="mt-2 text-[13px] text-muted" style={{ fontFamily: BODY_FONT }}>
                      Add your first income or expense manually. Nothing is imported from a bank.
                    </p>
                    <div className="mt-4 flex items-center justify-center gap-2">
                      <button
                        onClick={() => setSheet({ mode: "create" })}
                        className="h-11 rounded-[16px] bg-[#1b2130] text-[#fafaf5] px-5 text-[13px] font-semibold inline-flex items-center gap-1.5"
                      >
                        <Plus size={15} /> Add transaction
                      </button>
                      <button
                        onClick={() => router.push("/mo")}
                        className="h-11 rounded-[16px] bg-inset text-primary px-4 text-[13px] font-semibold inline-flex items-center gap-1.5"
                      >
                        <Bot size={15} /> Ask Mo
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {transactions.slice(0, RECENT_LIMIT).map((tx) => (
                      <button
                        key={tx.id}
                        onClick={() => setSheet({ mode: "edit", tx })}
                        className="w-full bg-surface border border-line p-4 rounded-[20px] flex justify-between items-center text-left hover:bg-surface-2 transition-colors"
                      >
                        <div className="flex items-center gap-4 min-w-0">
                          <div className="w-10 h-10 bg-inset rounded-full flex items-center justify-center text-secondary shrink-0"><Receipt size={16} /></div>
                          <div className="min-w-0">
                            <p className="text-sm font-semibold text-primary truncate" style={{ fontFamily: BODY_FONT }}>{tx.description || tx.category}</p>
                            <p className="text-xs text-muted truncate" style={{ fontFamily: BODY_FONT }}>{tx.category} · {formatDate(tx.date)}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className={`text-sm font-semibold tabular-nums ${tx.type === "income" ? "text-success" : "text-primary"}`} style={{ fontFamily: BODY_FONT }}>
                            {tx.type === "income" ? "+" : "−"} {rm(tx.amount)}
                          </span>
                          <Pencil size={13} className="text-muted" />
                        </div>
                      </button>
                    ))}
                    {transactions.length > RECENT_LIMIT && (
                      <button
                        onClick={() => router.push("/finance/history")}
                        className="w-full text-center py-3 text-[13px] font-semibold text-secondary hover:text-primary"
                        style={{ fontFamily: BODY_FONT }}
                      >
                        View All ({transactions.length})
                      </button>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>

      </div>

      {sheet && (
        <TransactionSheet
          mode={sheet.mode}
          categories={categories.length ? categories : ["Other"]}
          initial={sheet.tx ?? null}
          onClose={() => setSheet(null)}
          onSaved={reload}
        />
      )}
    </div>
  );
}
