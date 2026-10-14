// B4 — appearance-system pure logic (Node's built-in runner).
// Run via `npm run test:node`. Excluded from tsconfig / eslint / the Next build.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  APPEARANCE_MODES,
  DEFAULT_MODE,
  isAppearanceMode,
  parseStoredMode,
  resolveTheme,
  effectiveMode,
  readDeviceMode,
  writeDeviceMode,
  systemPrefersDark,
  APPEARANCE_STORAGE_KEY,
} from "../appearance.ts";

test("the canonical modes are exactly system | light | dark", () => {
  assert.deepEqual([...APPEARANCE_MODES], ["system", "light", "dark"]);
  assert.equal(DEFAULT_MODE, "system");
});

test("isAppearanceMode validates the three values and rejects everything else", () => {
  for (const m of APPEARANCE_MODES) assert.equal(isAppearanceMode(m), true);
  for (const bad of ["System", "DARK", "auto", "oled", "", "  ", null, undefined, 1, {}, ["dark"]]) {
    assert.equal(isAppearanceMode(bad), false);
  }
});

test("parseStoredMode tolerates bare, JSON-quoted, spaced and mixed-case values", () => {
  assert.equal(parseStoredMode("dark"), "dark");
  assert.equal(parseStoredMode('"light"'), "light");
  assert.equal(parseStoredMode("  System  "), "system");
  assert.equal(parseStoredMode("'dark'"), "dark");
});

test("parseStoredMode returns null for corrupt / unknown / non-string input", () => {
  for (const bad of ['{"mode":"dark"}', "darkk", "", "   ", "true", null, undefined, 42, {}, []]) {
    assert.equal(parseStoredMode(bad as unknown), null);
  }
});

test("resolveTheme: light/dark ignore the OS; system follows it", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("light", false), "light");
  assert.equal(resolveTheme("dark", false), "dark");
  assert.equal(resolveTheme("dark", true), "dark");
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
});

test("effectiveMode precedence: device before auth, account after auth, device when account has none", () => {
  // before authenticated settings resolve -> device choice (or System)
  assert.equal(effectiveMode({ deviceMode: "dark", serverMode: "light", authResolved: false }), "dark");
  assert.equal(effectiveMode({ deviceMode: null, serverMode: "light", authResolved: false }), "system");
  // authenticated WITH a saved account appearance -> that wins (follows the account)
  assert.equal(effectiveMode({ deviceMode: "dark", serverMode: "light", authResolved: true }), "light");
  // authenticated but the account never saved one -> keep the device choice
  assert.equal(effectiveMode({ deviceMode: "dark", serverMode: null, authResolved: true }), "dark");
  assert.equal(effectiveMode({ deviceMode: null, serverMode: null, authResolved: true }), "system");
});

test("readDeviceMode: missing storage -> System; corrupt value -> System; valid value -> that", () => {
  const g = globalThis as unknown as { localStorage?: unknown };
  const original = g.localStorage;

  delete g.localStorage;                       // storage entirely unavailable
  assert.equal(readDeviceMode(), "system");

  const store = new Map<string, string>();
  g.localStorage = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
  assert.equal(readDeviceMode(), "system");    // nothing stored
  store.set(APPEARANCE_STORAGE_KEY, "not-a-mode");
  assert.equal(readDeviceMode(), "system");    // corrupt -> default
  store.set(APPEARANCE_STORAGE_KEY, "dark");
  assert.equal(readDeviceMode(), "dark");
  writeDeviceMode("light");
  assert.equal(store.get(APPEARANCE_STORAGE_KEY), "light");

  if (original === undefined) delete g.localStorage;
  else g.localStorage = original;
});

test("readDeviceMode / writeDeviceMode swallow a throwing storage accessor", () => {
  const g = globalThis as unknown as { localStorage?: unknown };
  const original = g.localStorage;
  g.localStorage = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
  };
  assert.equal(readDeviceMode(), "system");
  assert.doesNotThrow(() => writeDeviceMode("dark"));
  if (original === undefined) delete g.localStorage;
  else g.localStorage = original;
});

test("systemPrefersDark: no matchMedia -> false (safe light default)", () => {
  const g = globalThis as unknown as { matchMedia?: unknown };
  const original = g.matchMedia;
  delete g.matchMedia;
  assert.equal(systemPrefersDark(), false);
  g.matchMedia = () => { throw new Error("nope"); };
  assert.equal(systemPrefersDark(), false);
  g.matchMedia = (q: string) => ({ matches: q.includes("dark") });
  assert.equal(systemPrefersDark(), true);
  if (original === undefined) delete g.matchMedia;
  else g.matchMedia = original;
});
