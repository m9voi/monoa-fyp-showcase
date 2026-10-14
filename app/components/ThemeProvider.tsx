"use client";

import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";
import { useAuth } from "../lib/auth";
import { apiFetch } from "../lib/api";
import {
  type AppearanceMode, type ResolvedTheme,
  isAppearanceMode, readDeviceMode, writeDeviceMode,
  resolveTheme, systemPrefersDark,
} from "../lib/appearance";

type SetResult = { serverSaved: boolean | null };  // null = not applicable (signed out)

type AppearanceCtx = {
  mode: AppearanceMode;                 // the canonical selected mode in effect
  resolvedTheme: ResolvedTheme;         // what actually paints right now
  source: "device" | "account";        // where `mode` came from
  setMode: (m: AppearanceMode) => Promise<SetResult>;
};

const Ctx = createContext<AppearanceCtx | null>(null);

// B11.1 — the SAME provider is the single settings store. These two account
// preferences are now reactive (not a private ref): the reminders surface reads
// `notifications`, and `useDeviceOrigin` reads `locationServices` to genuinely
// gate navigator.geolocation. `ready` is false until the authenticated account's
// settings have been ingested, so a caller never gates on a stale default.
type SettingsCtx = {
  notifications: boolean;
  locationServices: boolean;
  ready: boolean;
  setPref: (patch: Partial<{ notifications: boolean; locationServices: boolean }>) => Promise<SetResult>;
};

const SettingsContext = createContext<SettingsCtx | null>(null);

function applyDom(mode: AppearanceMode) {
  try {
    const el = document.documentElement;
    if (mode === "light" || mode === "dark") el.setAttribute("data-theme", mode);
    else el.removeAttribute("data-theme");
  } catch {
    /* SSR / no document */
  }
}

export default function ThemeProvider({ children }: { children: React.ReactNode }) {
  const { status, user } = useAuth();

  // Lazy-initialised from device storage — matches what the pre-hydration
  // bootstrap script already applied to <html>, so no flash and no SSR/client
  // state split that would render differently.
  const [mode, setModeState] = useState<AppearanceMode>(() => readDeviceMode());
  const [source, setSource] = useState<"device" | "account">("device");
  const [sysDark, setSysDark] = useState<boolean>(() => systemPrefersDark());
  // B11.1 — the two account toggles, now reactive. `otherPrefs` mirrors them so
  // an appearance change can re-send the WHOLE settings body unchanged.
  const [notifications, setNotifications] = useState(true);
  const [locationServices, setLocationServices] = useState(true);
  const [settingsReady, setSettingsReady] = useState(false);
  const otherPrefs = useRef<{ notifications: boolean; location_services: boolean }>({
    notifications: true, location_services: true,
  });

  // Re-assert the device choice on <html> once mounted (defensive; the inline
  // bootstrap already did this). Side effect only — no state.
  useEffect(() => {
    applyDom(readDeviceMode());
  }, []);

  // Track the OS scheme while on System. The paint is pure CSS (data-theme is
  // unset for System), so this listener only refreshes the value the Settings
  // screen reads back ("Resolved appearance: …").
  useEffect(() => {
    let mq: MediaQueryList | null = null;
    try {
      if (typeof matchMedia === "function") mq = matchMedia("(prefers-color-scheme: dark)");
    } catch {
      mq = null;
    }
    if (!mq) return;
    const onChange = () => setSysDark(mq!.matches);
    try { mq.addEventListener("change", onChange); }
    catch { mq.addListener(onChange); }  // older Safari
    return () => {
      try { mq!.removeEventListener("change", onChange); }
      catch { mq!.removeListener(onChange); }
    };
  }, []);

  // Ingest the authenticated account's saved appearance; on sign-out or an
  // account switch, fall back to THIS device's own choice — one user's
  // preference is never carried into another session. This effect deliberately
  // syncs rendered state + <html> to the auth transition (an external system).
  useEffect(() => {
    let cancelled = false;

    if (status !== "authenticated" || !user) {
      const d = readDeviceMode();
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync rendered theme + <html> to sign-out
      setModeState(d);
      setSource("device");
      applyDom(d);
      // B11.1 — a signed-out (or transitioning) session has no account prefs to
      // gate on: reset to the safe defaults and mark not-ready so no reminder
      // surface or location gate acts on a prior user's values.
      setNotifications(true);
      setLocationServices(true);
      setSettingsReady(false);
      otherPrefs.current = { notifications: true, location_services: true };
      return;
    }

    (async () => {
      try {
        const s = await apiFetch<{
          notifications?: boolean; location_services?: boolean; appearance_mode?: string;
        }>("/api/settings", { kind: "crud" });
        if (cancelled) return;
        // /api/settings returns these as SQLite integers (0/1) or absent for a
        // brand-new account — coerce to a real boolean so the location gate's
        // strict `=== false` check works and the reminders surface reads true.
        const asBool = (v: unknown, dflt: boolean): boolean => (v == null ? dflt : Boolean(v));
        const nextNotifications = asBool(s.notifications, true);
        const nextLocation = asBool(s.location_services, true);
        otherPrefs.current = {
          notifications: nextNotifications,
          location_services: nextLocation,
        };
        setNotifications(nextNotifications);
        setLocationServices(nextLocation);
        setSettingsReady(true);
        if (isAppearanceMode(s.appearance_mode)) {
          setModeState(s.appearance_mode);
          setSource("account");
          applyDom(s.appearance_mode);
        } else {
          const d = readDeviceMode();  // account has no saved appearance -> device choice
          setModeState(d);
          setSource("device");
          applyDom(d);
        }
      } catch {
        /* settings unreachable — keep whatever is applied (the device choice) */
      }
    })();

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, user?.id]);

  const setMode = useCallback<AppearanceCtx["setMode"]>(async (m) => {
    setModeState(m);
    applyDom(m);
    writeDeviceMode(m);
    setSource(status === "authenticated" ? "account" : "device");
    setSysDark(systemPrefersDark());

    if (status !== "authenticated") return { serverSaved: null };
    try {
      await apiFetch("/api/settings/update", {
        method: "POST",
        kind: "crud",
        body: JSON.stringify({
          notifications: otherPrefs.current.notifications,
          location_services: otherPrefs.current.location_services,
          appearance_mode: m,
        }),
      });
      return { serverSaved: true };
    } catch {
      // still applied + device-persisted (a consistent visible state); the
      // caller surfaces a truthful "not synced to your account" message.
      setSource("device");
      return { serverSaved: false };
    }
  }, [status]);

  // B11.1 — change an account toggle: apply locally at once, then persist the
  // WHOLE settings body (mirrors setMode). Never triggers a location prompt.
  const setPref = useCallback<SettingsCtx["setPref"]>(async (patch) => {
    const nextNotifications = patch.notifications ?? notifications;
    const nextLocation = patch.locationServices ?? locationServices;
    setNotifications(nextNotifications);
    setLocationServices(nextLocation);
    otherPrefs.current = {
      notifications: nextNotifications,
      location_services: nextLocation,
    };
    if (status !== "authenticated") return { serverSaved: null };
    try {
      await apiFetch("/api/settings/update", {
        method: "POST",
        kind: "crud",
        body: JSON.stringify({
          notifications: nextNotifications,
          location_services: nextLocation,
          appearance_mode: mode,
        }),
      });
      return { serverSaved: true };
    } catch {
      return { serverSaved: false };
    }
  }, [notifications, locationServices, status, mode]);

  const resolvedTheme: ResolvedTheme = resolveTheme(mode, sysDark);

  const value = useMemo<AppearanceCtx>(
    () => ({ mode, resolvedTheme, source, setMode }),
    [mode, resolvedTheme, source, setMode],
  );

  const settingsValue = useMemo<SettingsCtx>(
    () => ({ notifications, locationServices, ready: settingsReady, setPref }),
    [notifications, locationServices, settingsReady, setPref],
  );

  return (
    <Ctx.Provider value={value}>
      <SettingsContext.Provider value={settingsValue}>{children}</SettingsContext.Provider>
    </Ctx.Provider>
  );
}

export function useAppearance(): AppearanceCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAppearance must be used within ThemeProvider");
  return v;
}

/** B11.1 — the reactive account preferences (notifications + location services).
 *  Same provider as useAppearance. Safe outside the provider (e.g. a public
 *  route): returns the neutral defaults with `ready: false` and a no-op setter. */
export function useSettings(): SettingsCtx {
  return (
    useContext(SettingsContext) ?? {
      notifications: true,
      locationServices: true,
      ready: false,
      setPref: async () => ({ serverSaved: null }),
    }
  );
}
