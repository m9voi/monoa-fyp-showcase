"use client";
import React, { useMemo, useState } from "react";
import Link from "next/link";
import { ChevronLeft, Receipt, Pencil } from "lucide-react";
import { useApiData } from "../../lib/useApi";
import { rm } from "../../lib/money";
import type { TransactionHistory, FinanceMeta, Transaction } from "../../lib/types";
import TransactionSheet from "../TransactionSheet";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

export default function TransactionHistoryPage() {
  const { data: meta } = useApiData<FinanceMeta>("/api/finance/meta");
  const [typeFilter, setTypeFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [sheetTx, setSheetTx] = useState<Transaction | null>(null);

  const qs = useMemo(() => {
    const p = new URLSearchParams();
    if (typeFilter) p.set("type", typeFilter);
    if (categoryFilter) p.set("category", categoryFilter);
    const s = p.toString();
    return s ? `?${s}` : "";
  }, [typeFilter, categoryFilter]);

  const { data, error, loading, reload } = useApiData<TransactionHistory>(`/api/finance/history${qs}`);
  const transactions = data?.transactions ?? [];

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-MY", {
      year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  };

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header className="flex items-center gap-4">
            <Link href="/finance" aria-label="Back to Wallet" className="h-10 w-10 bg-surface border border-line rounded-full flex items-center justify-center text-secondary hover:text-primary transition-colors">
              <ChevronLeft size={20} />
            </Link>
            <h1
              className="text-[22px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Transaction History
            </h1>
          </header>

          <div className="flex gap-2">
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              aria-label="Filter by type"
              className="flex-1 bg-surface border border-line rounded-[14px] px-3 py-2.5 text-xs text-primary outline-none"
              style={{ fontFamily: BODY_FONT }}
            >
              <option value="">All types</option>
              {(meta?.types ?? ["expense", "income"]).map((t) => (
                <option key={t} value={t} className="capitalize">{t}</option>
              ))}
            </select>
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              aria-label="Filter by category"
              className="flex-1 bg-surface border border-line rounded-[14px] px-3 py-2.5 text-xs text-primary outline-none"
              style={{ fontFamily: BODY_FONT }}
            >
              <option value="">All categories</option>
              {(meta?.categories ?? []).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>

          {loading && <p className="text-muted text-sm px-2 text-center py-8" style={{ fontFamily: BODY_FONT }}>Loading transactions…</p>}

          {!loading && error && (
            <p className="text-danger text-sm px-2 text-center py-8 bg-danger/10 border border-danger/20 rounded-[20px]" style={{ fontFamily: BODY_FONT }}>
              Couldn&apos;t load your transactions. Please try again.
            </p>
          )}

          {!loading && !error && transactions.length === 0 && (
            <p className="text-muted text-sm px-2 text-center py-8 bg-surface border border-line rounded-[20px]" style={{ fontFamily: BODY_FONT }}>
              {typeFilter || categoryFilter ? "No transactions match those filters." : "No transactions recorded yet."}
            </p>
          )}

          {!loading && !error && transactions.length > 0 && (
            <div className="space-y-2">
              {transactions.map((tx) => (
                <button
                  key={tx.id}
                  onClick={() => setSheetTx(tx)}
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
            </div>
          )}
        </div>
      </div>

      {sheetTx && (
        <TransactionSheet
          mode="edit"
          categories={meta?.categories?.length ? meta.categories : ["Other"]}
          initial={sheetTx}
          onClose={() => setSheetTx(null)}
          onSaved={reload}
        />
      )}
    </div>
  );
}
