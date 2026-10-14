"use client";
import React, { useState } from "react";
import Link from "next/link";
import { ChevronLeft, ChevronRight, Check, Monitor, Sun, Moon } from "lucide-react";
import { ApiError } from "../lib/api";
import { useApiData } from "../lib/useApi";
import { useAppearance, useSettings } from "../components/ThemeProvider";
import { APPEARANCE_MODES, type AppearanceMode } from "../lib/appearance";
import type { UserPreferences } from "../lib/types";

// Phase 07G (Figma 261:168 "Settings — Overview"): the frame's row set
// (Appearance / Notifications / Language & region / Accessibility /
// Mo & Personalization / Privacy & data / Permissions / Account & security)
// is a materially richer control surface than this build supports. Only
// `appearance_mode`, `notifications`, and `location_services` are real
// (main.py's /api/settings + /api/settings/update). Language & region,
// Accessibility, a distinct "Mo & Personalization" destination, and
// stored-data export do not exist and are not represented here — building a
// row with a chevron to nowhere would be exactly the "dead control" this
// batch's task explicitly forbids. Who Am I, Personal Details, and (as of
// the account-security batch) Account & Security ARE real, so they're
// linked from here too, matching this task's own suggested information
// architecture.
const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

type ToggleKey = "notifications" | "location_services";

const MODE_META: Record<AppearanceMode, { label: string; icon: React.ReactNode }> = {
  system: { label: "System", icon: <Monitor size={18} /> },
  light: { label: "Light", icon: <Sun size={18} /> },
  dark: { label: "Dark", icon: <Moon size={18} /> },
};

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[18px] font-semibold text-primary px-1" style={{ fontFamily: BODY_FONT }}>
      {children}
    </h2>
  );
}

function NavRow({ href, title, subtitle }: { href: string; title: string; subtitle: string }) {
  return (
    <Link
      href={href}
      className="w-full bg-surface border border-line p-4 rounded-[16px] flex items-center justify-between hover:bg-surface-2 transition-colors min-h-[56px]"
    >
      <div className="min-w-0">
        <p className="text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{title}</p>
        <p className="text-[11px] text-muted mt-0.5" style={{ fontFamily: BODY_FONT }}>{subtitle}</p>
      </div>
      <ChevronRight size={20} className="text-muted shrink-0" />
    </Link>
  );
}

export default function SettingsPage() {
  const { data, error: loadError, loading } = useApiData<UserPreferences>("/api/settings");
  const { mode, resolvedTheme, setMode } = useAppearance();
  // B11.1 — the toggles now write through the single settings store so the
  // reminders surface and the location gate react immediately.
  const { notifications, locationServices, ready, setPref } = useSettings();

  const [pending, setPending] = useState<Partial<Record<ToggleKey, boolean>>>({});
  const [saving, setSaving] = useState(false);
  const [appBusy, setAppBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; tone: "ok" | "warn" | "err" } | null>(null);

  const storeValue = (key: ToggleKey): boolean =>
    key === "notifications" ? notifications : locationServices;
  const toggleValue = (key: ToggleKey): boolean =>
    pending[key] ?? storeValue(key) ?? (data ? Boolean(data[key]) : true);

  const flash = (text: string, tone: "ok" | "warn" | "err") => {
    setFeedback({ text, tone });
    if (tone === "ok") setTimeout(() => setFeedback(null), 2200);
  };

  const handleToggle = async (key: ToggleKey) => {
    const nextValue = !toggleValue(key);
    setPending((p) => ({ ...p, [key]: nextValue }));
    setSaving(true);
    try {
      const patch =
        key === "notifications" ? { notifications: nextValue } : { locationServices: nextValue };
      const { serverSaved } = await setPref(patch);
      flash(
        serverSaved === false
          ? "Saved on this device — couldn't sync to your account."
          : "Saved",
        serverSaved === false ? "warn" : "ok",
      );
    } catch (err) {
      setPending((p) => ({ ...p, [key]: !nextValue })); // revert on failure
      flash(err instanceof ApiError ? err.message : "Couldn't save. Please try again.", "err");
    } finally {
      setSaving(false);
    }
  };

  const chooseMode = async (m: AppearanceMode) => {
    if (m === mode || appBusy) return;
    setAppBusy(true);
    try {
      const { serverSaved } = await setMode(m);
      flash(
        serverSaved === false
          ? "Saved on this device — couldn't sync to your account."
          : "Saved",
        serverSaved === false ? "warn" : "ok",
      );
    } finally {
      setAppBusy(false);
    }
  };

  const toggleRow = (
    key: ToggleKey,
    title: string,
    subtitle: string,
    label: string,
  ) => {
    const rowReady = ready || Boolean(data);
    if (!rowReady && loading) {
      return <div className="h-[72px] rounded-[16px] bg-inset animate-pulse" aria-hidden="true" />;
    }
    const on = toggleValue(key);
    return (
      <div className="bg-surface border border-line rounded-[16px] p-4 flex items-center justify-between gap-3 min-h-[56px]">
        <div className="min-w-0">
          <p className="text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{title}</p>
          <p className="text-[11px] text-muted mt-0.5" style={{ fontFamily: BODY_FONT }}>{subtitle}</p>
        </div>
        <div className="flex items-center gap-2.5 shrink-0">
          <span className="text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>{on ? "On" : "Off"}</span>
          <button
            disabled={saving}
            onClick={() => handleToggle(key)}
            role="switch"
            aria-checked={on}
            aria-label={label}
            className={`w-12 h-6 flex items-center rounded-full p-1 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
              on ? "bg-success justify-end" : "bg-inset justify-start"
            }`}
          >
            <div className="bg-strong-fg w-4 h-4 rounded-full shadow-md" />
          </button>
        </div>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-6"
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
            <div className="min-w-0">
              <h1
                className="text-[22px] leading-[1.15] font-extrabold text-primary"
                style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
              >
                Settings
              </h1>
            </div>
          </header>

          {loadError && (
            <p role="alert" className="text-sm text-warning bg-warning/10 border border-warning/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
              Couldn&apos;t refresh your settings just now — showing your last known choices.
            </p>
          )}

          {feedback && (
            <p
              role="alert"
              className={`flex items-center gap-1.5 text-sm rounded-[16px] px-3 py-2 border ${
                feedback.tone === "ok"
                  ? "text-success bg-success/10 border-success/20"
                  : feedback.tone === "warn"
                  ? "text-warning bg-warning/10 border-warning/20"
                  : "text-danger bg-danger/10 border-danger/20"
              }`}
              style={{ fontFamily: BODY_FONT }}
            >
              {feedback.tone === "ok" && <Check size={14} />} {feedback.text}
            </p>
          )}

          {/* --- Experience --- */}
          <div className="space-y-2.5">
            <SectionLabel>Experience</SectionLabel>

            <fieldset className="bg-surface border border-line rounded-[20px] p-4">
              <legend className="text-[14px] font-semibold text-primary px-1" style={{ fontFamily: BODY_FONT }}>Appearance</legend>
              <p className="text-[11px] text-muted mt-0.5 px-1" style={{ fontFamily: BODY_FONT }}>
                System follows your device&rsquo;s light or dark setting and updates when it changes.
              </p>

              <div role="radiogroup" aria-label="Appearance" className="mt-3 grid grid-cols-3 gap-2">
                {APPEARANCE_MODES.map((m) => {
                  const meta = MODE_META[m];
                  const selected = mode === m;
                  return (
                    <label
                      key={m}
                      className={`relative flex flex-col items-center justify-center gap-1.5 min-h-[76px] rounded-[16px] border px-2 py-3 cursor-pointer transition-colors focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ring ${
                        selected
                          ? "border-[#1b2130] bg-[#1b2130] text-[#fafaf5]"
                          : "border-line bg-app text-secondary hover:bg-surface-2"
                      }`}
                    >
                      <input
                        type="radio"
                        name="appearance"
                        value={m}
                        checked={selected}
                        disabled={appBusy}
                        onChange={() => chooseMode(m)}
                        className="sr-only"
                      />
                      <span aria-hidden="true">{meta.icon}</span>
                      <span className="text-sm font-medium" style={{ fontFamily: BODY_FONT }}>{meta.label}</span>
                      {selected && <Check size={14} className="absolute top-1.5 right-1.5 text-[#fafaf5]" />}
                    </label>
                  );
                })}
              </div>

              <dl className="mt-3 text-xs space-y-0.5 px-1">
                <div className="flex gap-2">
                  <dt className="text-muted" style={{ fontFamily: BODY_FONT }}>Selected mode</dt>
                  <dd className="capitalize text-primary" style={{ fontFamily: BODY_FONT }}>{mode}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="text-muted" style={{ fontFamily: BODY_FONT }}>Resolved appearance</dt>
                  <dd className="capitalize text-primary" style={{ fontFamily: BODY_FONT }}>{resolvedTheme}</dd>
                </div>
              </dl>
            </fieldset>

            {toggleRow(
              "notifications",
              "Notifications",
              "Shows due task reminders while you're using or reopening MONOA. Background push (with the app closed) is not supported.",
              "Toggle in-app reminders",
            )}
          </div>

          {/* --- Privacy & data --- */}
          <div className="space-y-2.5">
            <SectionLabel>Privacy &amp; data</SectionLabel>
            {toggleRow(
              "location_services",
              "Location Services",
              "When off, MONOA never asks for your current location — Activity and Map use a place you search for instead. This doesn't change your device or browser location permission.",
              "Toggle location services",
            )}
          </div>

          {/* --- Personalization --- */}
          <div className="space-y-2.5">
            <SectionLabel>Personalization</SectionLabel>
            <NavRow href="/profile/who-am-i" title="Who Am I?" subtitle="Games, sports, movies & TV" />
          </div>

          {/* --- Account --- */}
          <div className="space-y-2.5">
            <SectionLabel>Account</SectionLabel>
            <NavRow href="/profile/personal-details" title="Personal details" subtitle="Your name" />
            <NavRow href="/settings/account-security" title="Account & security" subtitle="Password, email, delete account" />
          </div>
        </div>
      </div>
    </div>
  );
}
