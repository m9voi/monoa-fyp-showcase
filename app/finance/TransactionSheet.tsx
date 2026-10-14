"use client";

import React, { useState } from "react";
import { X, Trash2 } from "lucide-react";
import { apiFetch, ApiError, mutationOutcome, describeFailure } from "../lib/api";
import { newClientToken } from "../lib/clientToken";
import { usePendingCreate } from "../lib/usePendingCreate";
import type { Transaction } from "../lib/types";

const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

type Mode = "create" | "edit";

function toLocalInput(iso: string | undefined): string {
  // datetime-local wants "YYYY-MM-DDTHH:mm" in local time
  const d = iso ? new Date(iso) : new Date();
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function TransactionSheet({
  mode,
  categories,
  initial,
  onClose,
  onSaved,
}: {
  mode: Mode;
  categories: string[];
  initial?: Transaction | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [amount, setAmount] = useState(initial ? String(initial.amount) : "");
  const [type, setType] = useState<"expense" | "income">(initial?.type ?? "expense");
  // Income never shows the expense-category selector; a NEW income entry
  // uses the already-supported "Other" category internally (never an
  // invented one). An EXISTING transaction being edited keeps its own real
  // category on open — this only changes when the user DELIBERATELY toggles
  // Income/Expense below, never just from opening the form.
  const [category, setCategory] = useState(
    initial?.category ?? (type === "income" ? "Other" : categories[0] ?? "Other"),
  );
  const [description, setDescription] = useState(initial?.description ?? "");
  const [date, setDate] = useState(toLocalInput(initial?.date));
  const [busy, setBusy] = useState<null | "save" | "delete">(null);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Phase 5H-B3: one idempotency token for the lifetime of this open form, so a
  // retry after a slow / lost response cannot create a second transaction.
  // Generated once (lazy init); the sheet unmounts on close, so a re-open gets
  // a fresh token.
  const [clientToken] = useState<string>(newClientToken);
  const pending = usePendingCreate("transaction", clientToken);

  // A DELIBERATE Income/Expense toggle — not the initial render — is the
  // only thing allowed to change category here. Income always collapses to
  // "Other" (hidden from the UI); switching back to Expense restores a
  // valid expense category (whatever is already selected if it's still in
  // the list, otherwise the first one) rather than leaving "Other" stuck
  // from the income side if "Other" isn't itself a valid expense category.
  const changeType = (t: "expense" | "income") => {
    if (t === type) return;
    setType(t);
    if (t === "income") {
      setCategory("Other");
    } else {
      setCategory((prev) => (categories.includes(prev) ? prev : categories[0] ?? "Other"));
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const n = Number(amount);
    if (!Number.isFinite(n) || n <= 0) {
      setError("Enter an amount greater than zero.");
      return;
    }
    setBusy("save");
    setError("");
    const body = {
      amount: n,
      type,
      category,
      description: description.trim(),
      date: date ? new Date(date).toISOString() : undefined,
    };
    try {
      if (mode === "create") {
        pending.arm();
        await apiFetch("/api/transactions", {
          method: "POST",
          body: JSON.stringify({ ...body, client_token: clientToken }),
        });
        pending.resolve();
      } else if (initial) {
        await apiFetch(`/api/transactions/${initial.id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
      }
      onSaved();
      onClose();
    } catch (err) {
      const is409 = err instanceof ApiError && err.kind === "http" && err.status === 409;
      if (mode === "create" && is409) {
        // B3 idempotency conflict: this token ALREADY committed a create (with
        // different details). The earlier one is real — keep the pending record
        // so reconciliation still surfaces it; do NOT resolve().
        setError(
          "This was already saved a moment ago, with different details. Check your transactions and refresh if it looks wrong.",
        );
      } else if (mode === "create" && mutationOutcome(err) === "unknown") {
        // The write may have committed — never say it failed. The pending
        // record stays so a reload can reconcile it.
        setError(
          "We couldn't confirm whether this was saved. Check your transactions — it may already be there — before adding it again.",
        );
      } else {
        if (mode === "create") pending.resolve(); // definite 4xx: nothing was written
        setError(describeFailure(err, { verb: "save this" }));
      }
      setBusy(null);
    }
  };

  const doDelete = async () => {
    if (!initial || busy) return;
    setBusy("delete");
    setError("");
    try {
      await apiFetch(`/api/transactions/${initial.id}`, { method: "DELETE" });
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't delete. Please try again.");
      setBusy(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[100] bg-backdrop backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={mode === "create" ? "Add transaction" : "Edit transaction"}
    >
      <div
        className="w-full max-w-md max-h-[90vh] overflow-y-auto bg-surface border border-line rounded-t-[28px] sm:rounded-[28px] p-6 space-y-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-[17px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
            {mode === "create" ? "New transaction" : "Edit transaction"}
          </h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-primary p-1">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={submit} className="space-y-5">
          <div>
            <p className="text-[10px] font-semibold tracking-wide text-faint mb-2" style={{ fontFamily: BODY_FONT }}>TYPE &amp; AMOUNT</p>
            <div className="flex gap-2">
              {(["expense", "income"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => changeType(t)}
                  className={`flex-1 h-11 rounded-[16px] text-sm font-semibold capitalize border transition-colors ${
                    type === t
                      ? "bg-[#1b2130] text-[#fafaf5] border-[#1b2130]"
                      : "bg-surface text-secondary border-line hover:text-primary"
                  }`}
                  style={{ fontFamily: BODY_FONT }}
                >
                  {t}
                </button>
              ))}
            </div>
            <div className="mt-2 bg-field border border-field-border rounded-[18px] px-4 h-[58px] flex items-center">
              <span className="text-primary text-[15px] font-medium mr-1" style={{ fontFamily: BODY_FONT }}>RM</span>
              <input
                type="number"
                inputMode="decimal"
                min="0"
                step="0.01"
                required
                aria-label="Amount (RM)"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="flex-1 bg-transparent text-[16px] text-primary outline-none"
                placeholder="0.00"
                style={{ fontFamily: BODY_FONT }}
              />
            </div>
          </div>

          {type === "expense" && (
            <div>
              <p className="text-[10px] font-semibold tracking-wide text-faint mb-2" style={{ fontFamily: BODY_FONT }}>CATEGORY</p>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                aria-label="Category"
                className="w-full bg-field border border-field-border rounded-[18px] px-4 h-[58px] text-[15px] text-primary outline-none focus:border-ring"
                style={{ fontFamily: BODY_FONT }}
              >
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <p className="text-[10px] font-semibold tracking-wide text-faint mb-2" style={{ fontFamily: BODY_FONT }}>DETAILS</p>
            <div className="space-y-2.5">
              <input
                type="text"
                maxLength={140}
                aria-label={type === "income" ? "Source / note (optional)" : "Description (optional)"}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full bg-field border border-field-border rounded-[18px] px-4 h-[58px] text-[15px] text-primary outline-none focus:border-ring"
                placeholder={
                  type === "income"
                    ? "e.g. salary, allowance or freelance payment"
                    : "Note (optional) — e.g. lunch at campus"
                }
                style={{ fontFamily: BODY_FONT }}
              />
              <input
                type="datetime-local"
                aria-label="Date and time"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className="w-full bg-field border border-field-border rounded-[18px] px-4 h-[58px] text-[15px] text-primary outline-none focus:border-ring"
                style={{ fontFamily: BODY_FONT }}
              />
            </div>
          </div>

          {mode === "create" && pending.recovered && (
            <p className="text-sm text-warning bg-warning/10 border border-warning/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
              A transaction you started earlier had already been saved. You may not need to add this one.
            </p>
          )}

          {error && (
            <p className="text-sm text-danger bg-danger/10 border border-danger/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy !== null}
            className="w-full bg-[#1b2130] text-[#fafaf5] font-semibold py-3.5 rounded-[18px] disabled:opacity-50"
            style={{ fontFamily: BODY_FONT }}
          >
            {busy === "save"
              ? "Saving…"
              : mode === "create"
                ? (type === "income" ? "Add income" : "Add expense")
                : "Save changes"}
          </button>
        </form>

        {mode === "edit" && initial && (
          <div className="pt-2 border-t border-line">
            {!confirmDelete ? (
              <button
                onClick={() => setConfirmDelete(true)}
                className="w-full flex items-center justify-center gap-2 text-danger hover:opacity-80 text-sm py-2"
                style={{ fontFamily: BODY_FONT }}
              >
                <Trash2 size={15} /> Delete transaction
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-secondary text-center" style={{ fontFamily: BODY_FONT }}>
                  Delete this transaction? This cannot be undone.
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={() => setConfirmDelete(false)}
                    disabled={busy !== null}
                    className="flex-1 py-2 rounded-[16px] border border-line text-sm text-secondary"
                    style={{ fontFamily: BODY_FONT }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={doDelete}
                    disabled={busy !== null}
                    className="flex-1 py-2 rounded-[16px] bg-danger-solid text-danger-fg hover:opacity-90 text-sm font-medium disabled:opacity-50"
                    style={{ fontFamily: BODY_FONT }}
                  >
                    {busy === "delete" ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
