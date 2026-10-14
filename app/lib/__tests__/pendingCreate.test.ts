// Phase 5H-B5 — the opaque pending-create store (Node's runner).
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

// Minimal localStorage shim on `window` before importing the module.
class MemStorage {
  private m = new Map<string, string>();
  get length() {
    return this.m.size;
  }
  key(i: number) {
    return Array.from(this.m.keys())[i] ?? null;
  }
  getItem(k: string) {
    return this.m.has(k) ? this.m.get(k)! : null;
  }
  setItem(k: string, v: string) {
    this.m.set(k, String(v));
  }
  removeItem(k: string) {
    this.m.delete(k);
  }
  clear() {
    this.m.clear();
  }
}
(globalThis as { window?: unknown }).window = { localStorage: new MemStorage() };

const {
  rememberPendingCreate,
  forgetPendingCreate,
  listPendingCreates,
  clearAllPendingCreates,
  isDifferentAccount,
} = await import("../pendingCreate.ts");

beforeEach(() => {
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).clear();
});

test("remember then list returns the opaque record (token + kind + ts only)", () => {
  rememberPendingCreate("transaction", "tok-1");
  const all = listPendingCreates();
  assert.equal(all.length, 1);
  assert.equal(all[0].token, "tok-1");
  assert.equal(all[0].kind, "transaction");
  assert.equal(typeof all[0].ts, "number");
  // no other keys leaked in
  assert.deepEqual(Object.keys(all[0]).sort(), ["kind", "token", "ts"]);
});

test("forget removes one record", () => {
  rememberPendingCreate("task", "tok-a");
  rememberPendingCreate("event", "tok-b");
  forgetPendingCreate("tok-a");
  const all = listPendingCreates();
  assert.deepEqual(all.map((r) => r.token), ["tok-b"]);
});

test("malformed tokens are never stored", () => {
  rememberPendingCreate("transaction", "has space");
  rememberPendingCreate("transaction", "x".repeat(65));
  rememberPendingCreate("transaction", "bad/slash");
  assert.equal(listPendingCreates().length, 0);
});

test("expired records are pruned on list", () => {
  rememberPendingCreate("omni", "tok-old");
  const future = Date.now() + 16 * 60 * 1000; // > 15 min MAX_AGE
  assert.equal(listPendingCreates(future).length, 0);
  // and the key is actually gone
  assert.equal(listPendingCreates().length, 0);
});

test("corrupt entries are dropped, valid ones survive", () => {
  const ls = (globalThis as { window: { localStorage: MemStorage } }).window.localStorage;
  ls.setItem("coreos.pending.tok-good", JSON.stringify({ token: "tok-good", kind: "task", ts: Date.now() }));
  ls.setItem("coreos.pending.tok-bad", "{not json");
  ls.setItem("coreos.pending.tok-bad2", JSON.stringify({ nope: 1 }));
  const all = listPendingCreates();
  assert.deepEqual(all.map((r) => r.token), ["tok-good"]);
});

test("clearAll wipes every pending record (logout / account switch)", () => {
  rememberPendingCreate("transaction", "t1");
  rememberPendingCreate("task", "t2");
  rememberPendingCreate("event", "t3");
  clearAllPendingCreates();
  assert.equal(listPendingCreates().length, 0);
});

test("isDifferentAccount: only a VERIFIED switch (both ids known, different) is true", () => {
  assert.equal(isDifferentAccount("usr_a", "usr_b"), true); // A -> B switch
  assert.equal(isDifferentAccount("usr_a", "usr_a"), false); // same-user re-auth
  assert.equal(isDifferentAccount(undefined, "usr_b"), false); // cold reload, prev unknown
  assert.equal(isDifferentAccount(null, "usr_b"), false); // cold reload
  assert.equal(isDifferentAccount("usr_a", undefined), false); // -> anonymous (handled elsewhere)
  assert.equal(isDifferentAccount(null, null), false);
});

test("storage that throws on write -> silent no-op (private mode / quota)", () => {
  const w = (globalThis as { window: { localStorage: MemStorage } }).window;
  const realSet = w.localStorage.setItem.bind(w.localStorage);
  w.localStorage.setItem = () => {
    throw new DOMException("QuotaExceededError");
  };
  try {
    assert.doesNotThrow(() => rememberPendingCreate("task", "tok-quota"));
    assert.equal(listPendingCreates().length, 0);
  } finally {
    w.localStorage.setItem = realSet;
  }
});
