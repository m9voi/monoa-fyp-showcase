"use client";
// Who Am I — one category's select/deselect screen (Games / Sports /
// Movies & TV / General). Shared by the four thin route pages so each stays
// a real static route (matching this app's existing routing convention)
// without repeating the same fetch/select/save logic four times.
//
// Phase 07F (Figma 268:130 / 275:130 / 275:163 — Games/Sports/Movies & TV):
// each of those frames shows a free-text "search and add a verified result"
// flow for SPECIFIC titles/teams/shows (e.g. "FC Barcelona", "The Last of
// Us"), plus copy claiming results "surface in Today". Neither exists: the
// real allow-list (app/lib/interests.ts, mirrored server-side) is a bounded
// set of ~10 GENRE-level options per category, and interests only ever
// reach Mo's request context — never Today. The existing chip-toggle
// multi-select (already tested, already correct) was restyled with Figma's
// card/typography language instead of being replaced with a search UI that
// has no backend behind it. General has no dedicated Figma frame; it reuses
// this same restyled template per the task's own fallback instruction.

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { ChevronLeft, Check } from "lucide-react";
import { apiFetch, ApiError, describeFailure } from "../../lib/api";
import { useApiData } from "../../lib/useApi";
import { INTEREST_OPTIONS, type InterestCategory, type UserInterests } from "../../lib/interests";
import { toggleValue, isDirty } from "./interestSelection";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

export default function InterestCategoryScreen({
  category,
  title,
  subtitle,
}: {
  category: InterestCategory;
  title: string;
  subtitle: string;
}) {
  const { data, error: loadError, loading, reload } =
    useApiData<{ interests: UserInterests }>("/api/profile/interests");

  const [selected, setSelected] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);

  // Re-sync the editable selection to the real saved state whenever it
  // loads (or reloads after a save) — never trust only the locally-toggled
  // value as "saved".
  useEffect(() => {
    if (data) setSelected(data.interests[category]);
  }, [data, category]);

  const options = INTEREST_OPTIONS[category];
  const current = selected ?? [];
  const savedValue = data?.interests[category] ?? [];
  const dirty = isDirty(current, savedValue);

  const toggle = (value: string) => {
    setSaved(false);
    setSelected((prev) => toggleValue(prev ?? [], value));
  };

  const save = async () => {
    if (busy || !dirty) return;
    setBusy(true);
    setSaveError("");
    try {
      const res = await apiFetch<{ interests: UserInterests }>("/api/profile/interests", {
        method: "PUT",
        body: JSON.stringify({ [category]: current }),
      });
      setSelected(res.interests[category]);
      reload();
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
    } catch (err) {
      setSaveError(
        err instanceof ApiError ? describeFailure(err, { verb: "save this" }) : "Couldn't save. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header className="flex items-center gap-4">
            <Link
              href="/profile/who-am-i"
              aria-label="Back to Who Am I"
              className="w-10 h-10 bg-surface border border-line rounded-full flex items-center justify-center text-secondary hover:text-primary transition-colors shrink-0"
            >
              <ChevronLeft size={20} />
            </Link>
            <div className="min-w-0">
              <h1
                className="text-[22px] leading-[1.15] font-extrabold text-primary"
                style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
              >
                {title}
              </h1>
              <p className="text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>{subtitle}</p>
            </div>
          </header>

          {loading && !data && (
            <div className="flex flex-wrap gap-2" aria-hidden="true">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="h-11 w-24 rounded-full bg-inset animate-pulse" />
              ))}
            </div>
          )}

          {loadError && !data && (
            <p role="alert" className="text-sm text-danger bg-danger/10 border border-danger/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
              {loadError}
            </p>
          )}

          {data && (
            <>
              <div className="flex flex-wrap gap-2" role="group" aria-label={`${title} interests`}>
                {options.map((opt) => {
                  const isSelected = current.includes(opt);
                  return (
                    <button
                      key={opt}
                      type="button"
                      onClick={() => toggle(opt)}
                      aria-pressed={isSelected}
                      className={`min-h-[44px] px-4 rounded-full border text-sm font-medium transition-colors ${
                        isSelected
                          ? "bg-[#1b2130] text-[#fafaf5] border-[#1b2130]"
                          : "bg-surface text-secondary border-line hover:bg-surface-2"
                      }`}
                      style={{ fontFamily: BODY_FONT }}
                    >
                      {opt}
                    </button>
                  );
                })}
              </div>

              {saveError && (
                <p role="alert" className="text-sm text-danger bg-danger/10 border border-danger/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
                  {saveError}
                </p>
              )}
              {saved && !saveError && (
                <p className="flex items-center gap-1.5 text-sm text-success bg-success/10 border border-success/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
                  <Check size={14} /> Saved.
                </p>
              )}

              <button
                type="button"
                onClick={save}
                disabled={busy || !dirty}
                className="w-full bg-[#1b2130] text-[#fafaf5] font-semibold py-3.5 rounded-[18px] disabled:opacity-50"
                style={{ fontFamily: BODY_FONT }}
              >
                {busy ? "Saving…" : "Save changes"}
              </button>

              <div className="rounded-[20px] bg-inset px-4 py-3.5">
                <p className="text-[13px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>What this does</p>
                <p className="mt-1 text-[11px] text-secondary leading-relaxed" style={{ fontFamily: BODY_FONT }}>
                  Mo uses this as context when you ask about your week or for something to do.
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
