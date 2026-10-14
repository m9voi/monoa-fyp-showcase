// Launch / First-Run Entry — unit tests for the local "seen launch" marker.
// Run with Node's built-in test runner (zero deps): `npm run test:node`.

import { test } from "node:test";
import assert from "node:assert/strict";

import { hasSeenLaunch, markLaunchSeen } from "../launchSeen.ts";

// --------------------------------------------------------------------------- //
// global mocking helpers (Node has no `window` global by default)
// --------------------------------------------------------------------------- //
function setWindow(value: unknown) {
  Object.defineProperty(globalThis, "window", { value, configurable: true, writable: true });
}
function clearWindow() {
  Object.defineProperty(globalThis, "window", { value: undefined, configurable: true, writable: true });
}

function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
    clear: () => map.clear(),
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    get length() {
      return map.size;
    },
  } as Storage;
}

function throwingStorageAccessor(): { window: unknown } {
  // Simulates `window.localStorage` itself throwing (Safari private mode).
  return {
    window: {
      get localStorage(): Storage {
        throw new Error("SecurityError: storage disabled");
      },
    },
  };
}

function throwingStorage(): Storage {
  return {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {},
    clear: () => {},
    key: () => null,
    length: 0,
  } as Storage;
}

test("no window (SSR-like environment): hasSeenLaunch is false, markLaunchSeen no-ops", () => {
  clearWindow();
  assert.equal(hasSeenLaunch(), false);
  assert.doesNotThrow(() => markLaunchSeen());
});

test("first-time device: nothing written yet -> hasSeenLaunch is false", () => {
  setWindow({ localStorage: fakeStorage() });
  assert.equal(hasSeenLaunch(), false);
});

test("markLaunchSeen then hasSeenLaunch reflects it (round trip)", () => {
  setWindow({ localStorage: fakeStorage() });
  assert.equal(hasSeenLaunch(), false);
  markLaunchSeen();
  assert.equal(hasSeenLaunch(), true);
});

test("marker persists across independent reads (simulated refresh)", () => {
  const storage = fakeStorage();
  setWindow({ localStorage: storage });
  markLaunchSeen();
  // A fresh check against the SAME underlying storage (as a reload would see).
  setWindow({ localStorage: storage });
  assert.equal(hasSeenLaunch(), true);
});

test("unrelated/garbage value in the key is NOT treated as seen", () => {
  const storage = fakeStorage();
  storage.setItem("monoa.launchSeen.v1", "true"); // not the exact "1" sentinel
  setWindow({ localStorage: storage });
  assert.equal(hasSeenLaunch(), false);
});

test("storage.getItem throwing -> hasSeenLaunch fails safe to false, never throws", () => {
  setWindow({ localStorage: throwingStorage() });
  assert.doesNotThrow(() => {
    assert.equal(hasSeenLaunch(), false);
  });
});

test("storage.setItem throwing -> markLaunchSeen never throws (best-effort)", () => {
  setWindow({ localStorage: throwingStorage() });
  assert.doesNotThrow(() => markLaunchSeen());
});

test("window.localStorage accessor itself throwing -> both functions fail safe", () => {
  const w = throwingStorageAccessor();
  setWindow(w.window);
  assert.doesNotThrow(() => {
    assert.equal(hasSeenLaunch(), false);
  });
  assert.doesNotThrow(() => markLaunchSeen());
});

test("does not mark seen merely by checking — only markLaunchSeen commits it", () => {
  setWindow({ localStorage: fakeStorage() });
  hasSeenLaunch();
  hasSeenLaunch();
  hasSeenLaunch();
  assert.equal(hasSeenLaunch(), false, "reading the marker must never itself set it");
});

test.after(() => {
  clearWindow();
});
