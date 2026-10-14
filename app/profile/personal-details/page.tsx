"use client";

// Batch 01 — Personal Details. The one place the authenticated user can edit
// their real display name. Email is intentionally read-only here (it is the
// account's login identity). The users table stays the single source of
// truth: a successful save re-fetches the real session user (useAuth().refresh)
// instead of trusting the just-typed value, so this page — and every other
// surface reading useAuth().user (Profile, AvatarLink, Sidebar) — always
// reflects the backend's canonical name on its next normal read.
//
// Phase 07F (Figma 315:130): the frame's avatar-photo-edit link and its
// drill-down "Name ›" row imply an upload capability and a separate
// name-edit sub-screen — neither exists (no upload endpoint, no such
// route). Both were left out; the existing inline, already-verified name
// field is kept, restyled to Figma's card language instead.

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { ChevronLeft, Check } from "lucide-react";
import { useAuth } from "../../lib/auth";
import { apiFetch, ApiError, describeFailure } from "../../lib/api";
import type { AuthUser } from "../../lib/types";
import { NAME_MAX_LENGTH, validateName, isDirty, initials } from "../personalDetails";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

export default function PersonalDetailsPage() {
  const { user, refresh } = useAuth();
  const [name, setName] = useState(user?.name ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  // Re-sync the editable field to the real canonical name whenever it
  // changes underneath us (after a successful save's refresh(), or a
  // different account after AuthGuard's key={user.id} remount).
  useEffect(() => {
    setName(user?.name ?? "");
  }, [user?.name]);

  const dirty = isDirty(name, user?.name);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !dirty) return;
    const problem = validateName(name);
    if (problem) {
      setError(problem);
      return;
    }
    const trimmed = name.trim();
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      await apiFetch<{ user: AuthUser }>("/api/profile", {
        method: "PATCH",
        body: JSON.stringify({ name: trimmed }),
      });
      // Never show "saved" from the locally-typed value — re-fetch the real
      // session user so this page (and every other surface) shows exactly
      // what the backend persisted.
      await refresh();
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
    } catch (err) {
      setError(err instanceof ApiError ? describeFailure(err, { verb: "save this" }) : "Couldn't save. Please try again.");
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
              href="/profile"
              aria-label="Back to Profile"
              className="w-10 h-10 bg-surface border border-line rounded-full flex items-center justify-center text-secondary hover:text-primary transition-colors shrink-0"
            >
              <ChevronLeft size={20} />
            </Link>
            <h1
              className="text-[22px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Personal Details
            </h1>
          </header>

          <p className="text-[13px] text-muted" style={{ fontFamily: BODY_FONT }}>
            Your basic identity in MONOA.
          </p>

          <div className="rounded-[22px] bg-[#1b2130] px-5 py-5 flex items-center gap-3.5">
            <div className="h-[58px] w-[58px] shrink-0 rounded-full bg-accent-lav flex items-center justify-center text-[20px] font-semibold text-primary">
              {initials(user?.name, user?.email)}
            </div>
            <div className="min-w-0">
              <p className="text-[16px] font-semibold text-[#fbfaf6] truncate" style={{ fontFamily: BODY_FONT }}>{user?.name || "MONOA user"}</p>
              <p className="text-[12px] text-[#c2c4d1] truncate" style={{ fontFamily: BODY_FONT }}>{user?.email}</p>
            </div>
          </div>

          <form onSubmit={save} className="space-y-4">
            <p className="text-[10px] font-semibold tracking-wide text-faint px-1" style={{ fontFamily: BODY_FONT }}>PERSONAL INFO</p>

            <label className="block">
              <span className="text-xs text-muted" style={{ fontFamily: BODY_FONT }}>Name</span>
              <input
                type="text"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (error) setError("");
                }}
                maxLength={NAME_MAX_LENGTH}
                autoComplete="name"
                aria-label="Name"
                aria-describedby="personal-details-name-hint"
                className="mt-1 w-full bg-field border border-field-border rounded-[18px] px-4 h-[58px] text-[15px] text-primary outline-none focus:border-ring"
                style={{ fontFamily: BODY_FONT }}
              />
              <span id="personal-details-name-hint" className="mt-1 block text-[11px] text-faint" style={{ fontFamily: BODY_FONT }}>
                Shown across MONOA, including Today.
              </span>
            </label>

            <label className="block">
              <span className="text-xs text-muted" style={{ fontFamily: BODY_FONT }}>Email</span>
              <input
                type="email"
                value={user?.email ?? ""}
                readOnly
                aria-readonly="true"
                aria-label="Email (read-only)"
                className="mt-1 w-full bg-inset border border-field-border rounded-[18px] px-4 h-[58px] text-[15px] text-secondary outline-none cursor-default"
                style={{ fontFamily: BODY_FONT }}
              />
              <span className="mt-1 block text-[11px] text-faint" style={{ fontFamily: BODY_FONT }}>
                Can&apos;t be changed here — use <Link href="/settings/account-security" className="underline">Account &amp; Security</Link>.
              </span>
            </label>

            {error && (
              <p role="alert" className="text-sm text-danger bg-danger/10 border border-danger/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
                {error}
              </p>
            )}

            {saved && !error && (
              <p className="flex items-center gap-1.5 text-sm text-success bg-success/10 border border-success/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
                <Check size={14} /> Saved.
              </p>
            )}

            <button
              type="submit"
              disabled={busy || !dirty}
              className="w-full bg-[#1b2130] text-[#fafaf5] font-semibold py-3.5 rounded-[18px] disabled:opacity-50"
              style={{ fontFamily: BODY_FONT }}
            >
              {busy ? "Saving…" : "Save changes"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
