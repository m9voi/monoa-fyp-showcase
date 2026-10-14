// MONOA appearance system (B4) — pure logic, no React, no DOM writes.
// Canonical user-facing modes: system | light | dark.
// Resolved theme (what actually paints): light | dark.

export const APPEARANCE_MODES = ["system", "light", "dark"] as const;
export type AppearanceMode = (typeof APPEARANCE_MODES)[number];
export type ResolvedTheme = "light" | "dark";

export const APPEARANCE_STORAGE_KEY = "monoa.appearance";
export const DEFAULT_MODE: AppearanceMode = "system";

export function isAppearanceMode(v: unknown): v is AppearanceMode {
  return typeof v === "string" && (APPEARANCE_MODES as readonly string[]).includes(v);
}

/**
 * Parse a value read from device storage. Tolerates a bare string
 * ("dark"), a JSON-encoded string ('"dark"'), surrounding whitespace, and
 * case. Anything else — corrupt JSON, an object, an unknown word, null —
 * yields null so the caller falls back to the default.
 */
export function parseStoredMode(raw: unknown): AppearanceMode | null {
  if (typeof raw !== "string") return null;
  let s = raw.trim();
  if (!s) return null;
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    s = s.slice(1, -1).trim();
  }
  s = s.toLowerCase();
  return isAppearanceMode(s) ? s : null;
}

/** The theme a mode resolves to, given whether the OS currently prefers dark. */
export function resolveTheme(mode: AppearanceMode, systemPrefersDark: boolean): ResolvedTheme {
  if (mode === "light") return "light";
  if (mode === "dark") return "dark";
  return systemPrefersDark ? "dark" : "light";
}

/**
 * Precedence for which mode to apply:
 *   - before the authenticated settings have resolved -> the device choice
 *     (saved locally, else System);
 *   - once authenticated settings resolve WITH a saved appearance -> that
 *     server value (it follows the account across devices);
 *   - authenticated but the account has no saved appearance -> the device choice.
 * A signed-out session keeps the device choice.
 */
export function effectiveMode(input: {
  deviceMode: AppearanceMode | null;
  serverMode: AppearanceMode | null;
  authResolved: boolean;
}): AppearanceMode {
  const device = input.deviceMode ?? DEFAULT_MODE;
  if (input.authResolved && input.serverMode) return input.serverMode;
  return device;
}

// ---- guarded browser reads (safe in SSR, private mode, blocked storage) ----

export function readDeviceMode(): AppearanceMode {
  try {
    if (typeof localStorage === "undefined") return DEFAULT_MODE;
    return parseStoredMode(localStorage.getItem(APPEARANCE_STORAGE_KEY)) ?? DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function writeDeviceMode(mode: AppearanceMode): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(APPEARANCE_STORAGE_KEY, mode);
  } catch {
    /* storage unavailable / full / blocked — the in-memory choice still applies */
  }
}

export function systemPrefersDark(): boolean {
  try {
    return (
      typeof matchMedia === "function" &&
      matchMedia("(prefers-color-scheme: dark)").matches
    );
  } catch {
    return false;
  }
}
